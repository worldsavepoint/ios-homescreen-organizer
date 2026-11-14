"""Local stdio MCP server. Every edit tool changes the draft only; apply/restore write the device."""

from __future__ import annotations

import os
from typing import Annotated, Any, Callable

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import organize, preview, usage
from .config import DATA, session
from .model import Layout, LayoutError

ALLOW_APPLY = os.environ.get("HOMESCREEN_ALLOW_APPLY") == "1"

INSTRUCTIONS = """Arrange an iPhone Home Screen. Workflow: read_phone -> show_layout -> edit the
draft (arrange / move / create_folder / sort_* / file_into_folders ...) -> diff -> render_preview
-> apply only when the user explicitly asks. Every edit is one undo step (undo / redo).
Item ids: apps use their bundle id; folders and widgets use folder:N / widget:N.
Targets: 'dock', 'library', 'bank' (a holding tray; empty it before applying), 'page:N'
(1-based; N = last+1 makes a new page), 'folder:<id>[:N]'.
Move an app to 'library' to keep it in App Library; iOS restores apps omitted from the layout.
Read show_layout.grid for page cols/rows, Dock capacity (dock) and folder-page capacity (folder).
Pages hold cols * rows slots. Widget footprints are small=2x2, medium=4x2, large=4x4.
For fixed positions, inspect grid and call place_at with 1-based page/row/column coordinates.
Use move for sequential item order. For sequential arrange, call close_gaps on pinned items
first; arrange retains fixed positions for items staying on the same page. On iOS 16, use
these sequential-order edits.
Moving an item clears its fixed position. Dictionary reads retain exported fixed locations;
unreported/list observations use remembered positions. Apply uses the draft's positions.
Preserve page 1 and Dock placement unless the user requests changes. optimize_by_usage preserves
page-1 top-level placement when requested and optimizes folder contents throughout the draft,
including page-1, Dock and holding-tray folders. To sort by color, call icon_colors first;
use get_icons for icons needing visual review."""

mcp = MCPServer("ios-homescreen-mcp", instructions=INSTRUCTIONS)


def _edit(fn: Callable[[Layout], Any]) -> dict[str, Any]:
    """Run an edit as one undoable step; on error nothing changes."""
    s = session()
    try:
        out = s.edit(fn)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}
    res: dict[str, Any] = {"ok": True, "problems": s.require().validate()}
    if isinstance(out, dict):
        res.update(out)
    elif out is not None:
        res["result"] = out
    return res


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def read_phone() -> dict[str, Any]:
    """Read the selected iPhone source and return device details and item counts.
    Replaces the saved draft, clears undo/redo, and saves/prunes local read snapshots.
    Requires the selected source to be available."""
    return await session().read()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def show_layout() -> dict[str, Any]:
    """Return the loaded draft: Dock, pages, App Library and holding tray.
    Items include ids and names; folders include contents and widgets include sizes.
    grid supplies page cols/rows and Dock/folder-page capacities for layout edits.
    Requires a loaded draft. Session setup creates and sets permissions on the local data directory."""
    lay = session().require()
    return {**lay.summary(), "grid": lay.grid.to_dict()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def find_apps(
    query: Annotated[str, Field(description="Substring to match against loaded draft item names or bundle ids.")],
) -> list[dict[str, Any]]:
    """Search the loaded draft by name or bundle-id substring; return matching items and positions.
    Session setup creates and sets permissions on the local data directory."""
    lay = session().require()
    pos = lay.positions()
    return [{**n.describe(), "position": pos.get(n.id, "not placed")} for n in lay.find(query)]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=True))
async def icon_colors(
    ids: Annotated[list[str] | None, Field(description="Item ids from show_layout; omitted or empty selects every app with a bundle id.")] = None,
) -> dict[str, Any]:
    """Return results keyed by item id, with name and dominant icon family.
    Analyzed icons add hex, background and hue; unavailable or empty icons have family='unknown'.
    Reads uncached icons from the selected source and saves them locally."""
    return await session().icon_colors(ids)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=True))
async def get_icons(
    ids: Annotated[list[str], Field(description="Item ids from show_layout in display order; reads up to 24 items.")],
) -> list[str | Image]:
    """Return labels and available PNG images for the first 24 requested loaded draft items.
    Preserves request order; reads uncached icons from the selected source and saves them locally."""
    s = session()
    lay = s.require()
    out: list[str | Image] = []
    for i in ids[:24]:
        node = lay.get(i)
        png = await s.icon(node.bundle_id) if node.bundle_id else None
        out.append(f"{node.name} [{i}]")
        if png:
            out.append(Image(data=png, format="png"))
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def arrange(
    pages: Annotated[list[list[str]], Field(description="Pages in 1-based order; unpinned items flow in the supplied order. Clear same-page pins with close_gaps for row-major placement.")],
    dock: Annotated[list[str] | None, Field(description="Ordered Dock item ids; omitted leaves the Dock unchanged.")] = None,
    library: Annotated[list[str] | None, Field(description="App ids kept only in App Library; omitted leaves it unchanged.")] = None,
) -> dict[str, Any]:
    """Replace the loaded draft's top-level layout in one saved undo step.
    Every top-level item and App Library app must appear exactly once.
    Same-page items retain fixed positions; items moved between pages lose them.
    For sequential row-major placement, call close_gaps on pinned item ids before arranging.
    Omitted dock/library stay unchanged. Return ok and validation problems."""
    return _edit(lambda lay: lay.arrange(pages, dock, library))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def move(
    item_id: Annotated[str, Field(description="Item id from show_layout: app bundle id, folder:N or widget:N.")],
    target: Annotated[str, Field(description="dock, library, bank, page:N (1-based; last+1 creates a page), or folder:N[:P] (P defaults to 1).")],
    index: Annotated[int | None, Field(description="0-based insertion index, clamped to destination bounds; omitted appends.")] = None,
    before: Annotated[str | None, Field(description="Destination item id to insert before; takes precedence over index.")] = None,
) -> dict[str, Any]:
    """Move one loaded draft item and clear its fixed-position gaps, in one saved undo step.
    App Library removes an app from the Home Screen; empty the holding tray before apply.
    Return ok and validation problems."""
    return _edit(lambda lay: lay.move(item_id, target, index, before))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def create_folder(
    name: Annotated[str, Field(description="Display name for the new folder.")],
    item_ids: Annotated[list[str], Field(description="App item ids from the loaded draft, in the desired folder order.")],
    target: Annotated[str, Field(description="Top-level destination: dock, bank or page:N (1-based; last+1 creates a page).")],
    index: Annotated[int | None, Field(description="0-based destination insertion index; omitted appends.")] = None,
) -> dict[str, Any]:
    """Create a folder from loaded draft items in the supplied order, in one saved undo step.
    Return folder_id, ok and validation problems."""
    return _edit(lambda lay: {"folder_id": lay.create_folder(name, item_ids, target, index)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def rename_folder(
    folder_id: Annotated[str, Field(description="Folder id from show_layout, such as folder:1.")],
    name: Annotated[str, Field(description="Replacement folder display name.")],
) -> dict[str, Any]:
    """Rename a loaded draft folder in one saved undo step.
    Return ok and validation problems."""
    return _edit(lambda lay: lay.rename_folder(folder_id, name))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def dissolve_folder(
    folder_id: Annotated[str, Field(description="Folder id from show_layout, such as folder:1.")],
) -> dict[str, Any]:
    """Remove a loaded draft folder and put its apps in its former position, in one saved undo step.
    Return ok and validation problems."""
    return _edit(lambda lay: lay.dissolve_folder(folder_id))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def file_into_folders(
    groups: Annotated[dict[str, list[str]], Field(description="Mapping of folder display names to ordered loose app bundle ids.")],
    keep_first_page: Annotated[bool, Field(description="Leave apps on page 1 alone; defaults to true.")] = True,
) -> dict[str, Any]:
    """File loose loaded draft apps into named folders in one saved undo step.
    Join an existing page folder with that name or create one for at least two eligible apps.
    Return filed names/count, ok and validation problems."""
    return _edit(lambda lay: organize.into_folders(lay, groups, keep_first_page))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def sort_by_color(
    keep_first_page: Annotated[bool, Field(description="Leave page 1 unchanged; defaults to true.")] = True,
) -> dict[str, Any]:
    """Combine eligible draft pages, rainbow-sort loose apps after folders/widgets and repack them.
    Repacking clears gaps, moves folders/widgets across those pages and removes empty pages.
    Saves one undo step; reads and locally caches source icons as needed.
    Return ok and validation problems."""
    colors = await session().icon_colors()
    return _edit(lambda lay: organize.by_color(lay, colors, keep_first_page))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def sort_alphabetically(
    keep_first_page: Annotated[bool, Field(description="Leave page 1 unchanged; defaults to true.")] = True,
    folders_first: Annotated[bool, Field(description="Place folders before loose apps after widgets; defaults to true.")] = True,
) -> dict[str, Any]:
    """Combine eligible draft pages, sort items A-Z with widgets first and repack them.
    Repacking clears gaps, moves folders/widgets across those pages and removes empty pages.
    Saves one undo step; Dock and App Library stay unchanged.
    Return ok and validation problems."""
    return _edit(lambda lay: organize.alphabetical(lay, keep_first_page, folders_first))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def sort_folder_contents() -> dict[str, Any]:
    """Sort apps A-Z inside each placed loaded draft folder, in one saved undo step.
    Refills folder pages; return ok and validation problems."""
    return _edit(lambda lay: organize.sort_folder_contents(lay))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=True))
def app_store_categories(
    ids: Annotated[list[str] | None, Field(description="App bundle ids; omitted or empty selects every app in the loaded draft.")] = None,
) -> dict[str, str]:
    """Return App Store genres for loaded draft apps; Apple apps use Apple and misses use an empty string.
    Sends uncached bundle ids to Apple's public US lookup API and saves results locally.
    Requires user agreement to send bundle ids to Apple."""
    lay = session().require()
    ids = ids or [n.id for n in lay.nodes.values() if n.kind == "app"]
    return organize.fetch_categories(ids, DATA / "categories.json")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def place_at(
    item_id: Annotated[str, Field(description="Item id from show_layout: app bundle id, folder:N or widget:N.")],
    page: Annotated[int, Field(description="1-based page number; last+1 creates a page.")],
    row: Annotated[int, Field(description="1-based grid row, from 1 through show_layout.grid.rows.")],
    column: Annotated[int, Field(description="1-based grid column, from 1 through show_layout.grid.cols.")],
) -> dict[str, Any]:
    """Use show_layout.grid to pin a draft item at a page coordinate in one saved undo step.
    Fixed-position placement leaves gaps. Use move for sequential order, or close_gaps then arrange.
    On iOS 16, use these sequential-order edits.
    Other items flow around it; return ok and validation problems."""
    def run(lay: Layout) -> None:
        if not 1 <= row <= lay.grid.rows:
            raise LayoutError(f"row must be 1-{lay.grid.rows}")
        if not 1 <= column <= lay.grid.cols:
            raise LayoutError(f"column must be 1-{lay.grid.cols}")
        lay.pin(item_id, page, (row - 1) * lay.grid.cols + (column - 1))
    return _edit(run)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def close_gaps(
    item_ids: Annotated[list[str], Field(description="Item ids whose fixed-position pins should be removed.")],
) -> dict[str, Any]:
    """Remove fixed-position pins from loaded draft items in one saved undo step.
    Items flow with the page again; return ok and validation problems."""
    return _edit(lambda lay: [lay.unpin(i) for i in item_ids])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def swap(
    a: Annotated[str, Field(description="First loaded draft item id from show_layout.")],
    b: Annotated[str, Field(description="Second loaded draft item id from show_layout.")],
) -> dict[str, Any]:
    """Swap the positions and pins of two loaded draft items in one saved undo step.
    Return ok and validation problems."""
    return _edit(lambda lay: lay.swap(a, b))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def screen_time(
    days: Annotated[int, Field(description="Number of recent days for mac mode, default 30; device mode uses report coverage.")] = 30,
    source: Annotated[str, Field(description="device (default) reads device memory reports; mac reads synced macOS Screen Time.")] = "device",
) -> dict[str, Any]:
    """Return estimated top-app usage, background apps and phone-versus-draft page swipes.
    Requires a loaded draft and replaces cached usage. Device mode reads memory reports and
    installed apps, saving reports locally; mac mode reads synced macOS Screen Time files."""
    s = session()
    u = await s.load_device_usage() if source == "device" else s.load_usage(days)
    if not u.get("ok"):
        return u
    lay = s.require()
    return {"source": source, "days": u["days"], "background": u.get("background", []),
            "top": usage.top_apps(lay, u["opens"], 40),
            "swipes_on_phone": usage.swipes(lay.original(), u["opens"]),
            "swipes_in_draft": usage.swipes(lay, u["opens"])}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def optimize_by_usage(
    keep_first_page: Annotated[bool, Field(description="Preserve page 1 top-level placement; folder contents are optimized throughout the draft. Defaults to false.")] = False,
) -> dict[str, Any]:
    """Reorder loaded draft apps/folders by estimated usage in one saved undo step.
    Uses cached usage, else reads device reports into local files, then tries synced macOS
    Screen Time. Seeds repacked pages with their existing widgets; preserves Dock item placement and App Library.
    Sorts/refills every folder's contents, including page-1, Dock and holding-tray folders.
    Return ok and validation problems or an error."""
    s = session()
    if s.usage is None:
        try:
            await s.load_device_usage()
        except Exception:
            s.usage = None
        if not (s.usage or {}).get("ok"):
            s.load_usage()
    if not s.usage.get("ok"):
        return {"ok": False, "error": s.usage.get("error")}
    opens = s.usage["opens"]
    return _edit(lambda lay: usage.optimize(lay, opens, keep_first_page))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def page_op(
    op: Annotated[str, Field(description="Page operation: insert, move or delete.")],
    page: Annotated[int, Field(description="1-based page number; insert accepts last+1.")],
    to: Annotated[int | None, Field(description="Required 1-based destination page for move; otherwise unused.")] = None,
) -> dict[str, Any]:
    """Insert, move or delete a loaded draft page in one saved undo step.
    Delete sends its apps, including folder contents, to App Library; widgets prevent deletion.
    Return ok and validation problems."""
    def run(lay: Layout) -> None:
        if op == "insert":
            lay.insert_page(page)
        elif op == "move":
            if to is None:
                raise LayoutError("move needs `to`")
            lay.move_page(page, to)
        elif op == "delete":
            lay.delete_page(page)
        else:
            raise LayoutError(f"unknown op {op!r}")
    return _edit(run)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def remove_empty() -> dict[str, Any]:
    """Remove empty Home Screen pages and empty folders on those pages in one saved undo step.
    Trims empty child pages in page/Dock folders; retains Dock and holding-tray folder containers.
    Return ok and validation problems."""
    return _edit(lambda lay: lay.drop_empty())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def undo() -> dict[str, Any]:
    """Undo one available draft edit and persist the result.
    Return whether an edit was undone and the remaining undo/redo depths."""
    s = session()
    return {"ok": s.undo(), **s.history()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def redo() -> dict[str, Any]:
    """Redo one available undone draft edit and persist the result.
    Return whether an edit was redone and the remaining undo/redo depths."""
    s = session()
    return {"ok": s.redo(), **s.history()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=False))
def reset_draft() -> dict[str, Any]:
    """Replace the loaded draft with its last-read source layout in one saved undo step.
    Discards draft changes and returns ok."""
    session().reset()
    return {"ok": True}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def diff() -> dict[str, Any]:
    """Return loaded draft changes, validation problems and its current apply_token.
    Compares with the last-read layout.
    Session setup creates and sets permissions on the local data directory."""
    return session().require().diff()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=True, openWorldHint=True))
async def render_preview() -> dict[str, Any]:
    """Replace local preview.html with a draft-versus-last-read HTML preview; return its path.
    Requires a loaded draft; reads and locally caches source icons as needed."""
    path = await preview.render(session(), DATA / "preview.html")
    return {"path": str(path)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def apply(
    apply_token: Annotated[str, Field(description="Exact token from diff for the current draft; obtain a fresh token after edits.")],
) -> dict[str, Any]:
    """Write the loaded draft to the iPhone.
    Requires explicit user approval, HOMESCREEN_ALLOW_APPLY=1, a writable source and the latest diff token.
    Backs up first, checks identity and source changes, writes once and verifies; returns status.
    Replaces the local draft and clears history after readback."""
    if not ALLOW_APPLY:
        return {"ok": False, "error": "apply from the assistant is off. The user can apply in the app "
                f"(`home-screen-organizer ui`), run `home-screen-organizer apply {apply_token}`, or set HOMESCREEN_ALLOW_APPLY=1."}
    try:
        return await session().apply(apply_token)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=False))
def list_backups() -> list[str]:
    """Return saved backup filenames for this session's device, sorted by filename.
    Creates the local session/backup directory as needed and sets session-directory permissions."""
    return session().backups()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=False, openWorldHint=True))
async def restore(
    backup: Annotated[str, Field(description="Exact filename returned by list_backups for the previously read device.")],
) -> dict[str, Any]:
    """Write a named trusted backup to the iPhone.
    Requires explicit user approval, HOMESCREEN_ALLOW_APPLY=1, a writable source
    and a previously read device identity.
    Pinned backups require saved grid dimensions; trusted unpinned legacy backups remain supported.
    Restored item footprints must fit the loaded grid.
    Backs up first and verifies after writing; returns status, replaces the draft and clears history."""
    if not ALLOW_APPLY:
        return {"ok": False, "error": f"restore from the assistant is off. The user can run `home-screen-organizer restore {backup}`."}
    try:
        return await session().restore(backup)
    except LayoutError as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
