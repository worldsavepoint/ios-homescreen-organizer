"""Preview DOM positions match synthetic draft and original layout placements."""

import asyncio
from html.parser import HTMLParser

import pytest

from homescreen import preview
from homescreen.device import FileSource
from homescreen.model import Grid, Layout
from homescreen.session import Session
from fake_phone import app, folder, widget


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"img", "meta", "br", "hr", "input", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def elements(node, tag=None, cls=None):
    for child in node["children"]:
        if isinstance(child, dict):
            if (tag is None or child["tag"] == tag) and (
                    cls is None or cls in child["attrs"].get("class", "").split()):
                yield child
            yield from elements(child, tag, cls)


def text(node):
    return "".join(text(c) if isinstance(c, dict) else c for c in node["children"])


def styles(node):
    return dict(part.strip().split(":", 1) for part in node["attrs"].get("style", "").split(";") if part.strip())


@pytest.mark.parametrize("cols,rows", [(4, 6), (5, 7)])
def test_preview_renders_grid_pins_folder_and_widget_spans(tmp_path, cols, rows):
    state = [[], [app("com.example.a", "Pinned <A>"),
                  folder("Folder & More", [app("com.example.b", "B")]), widget("Weather", "small")]]
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.grid = Grid(cols=cols, rows=rows)
    session.layout = Layout(state, library=[], grid=session.grid)
    session.layout.pin("com.example.a", 1, cols * rows - 1)
    calls = []

    async def icon(bundle_id):
        calls.append(bundle_id)
        return None

    session.icon = icon
    path = asyncio.run(preview.render(session, tmp_path / "preview.html"))
    source = path.read_text()
    doc = Document(source)
    sections = {text(next(elements(n, tag="h3"))): n for n in elements(doc.root, cls="phone")}
    for title, expected in {
        "Draft · Page 1": {"Pinned <A>": (rows, cols, 1, 1), "Folder & More": (1, 1, 1, 1),
                           "Weather": (1, 3, 2, 2)},
        "Last read · Page 1": {"Pinned <A>": (1, 1, 1, 1), "Folder & More": (1, 2, 1, 1),
                             "Weather": (1, 3, 2, 2)},
    }.items():
        section = sections[title]
        assert styles(section)["--cols"] == str(cols)
        assert styles(section)["--rows"] == str(rows)
        cells = {text(n): n for n in next(elements(section, cls="grid"))["children"] if isinstance(n, dict)}
        for name, (row, column, width, height) in expected.items():
            assert styles(cells[name])["grid-row"] == f"{row} / span {height}"
            assert styles(cells[name])["grid-column"] == f"{column} / span {width}"
    assert "Pinned &lt;A&gt;" in source and "Folder &amp; More" in source
    assert calls == ["com.example.a", "com.example.b"]


@pytest.mark.parametrize("size,height", [("medium", 2), ("large", 4), ("extralarge", 4)])
def test_preview_uses_loaded_widget_footprint(tmp_path, size, height):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.layout = Layout([[], [widget("Weather", size)]], grid=Grid(cols=5, rows=7))
    path = asyncio.run(preview.render(session, tmp_path / "preview.html", show_original=False))
    node = next(elements(Document(path.read_text()).root, cls="widget"))
    assert styles(node) == {"grid-row": f"1 / span {height}", "grid-column": "1 / span 4"}


def test_preview_lists_items_outside_the_grid_with_escaped_placement_errors(tmp_path):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.layout = Layout([[], [widget("<Oversized>", "large")]], grid=Grid(cols=3, rows=3))
    path = asyncio.run(preview.render(session, tmp_path / "preview.html", show_original=False))
    source = path.read_text()
    doc = Document(source)
    assert "no room for &lt;Oversized&gt;" in source
    assert any(text(n) == "Unplaced items" for n in elements(doc.root, tag="h4"))
    assert text(next(elements(doc.root, cls="widget"))) == "<Oversized>"


def test_folder_thumbnail_reads_only_nine_visible_icons_across_both_snapshots(tmp_path):
    session = Session(FileSource(tmp_path / "unused.plist"), tmp_path / "data")
    session.layout = Layout([[], [folder("Many", [app(f"example.a{i}", f"A{i}") for i in range(108)])]])
    calls = []

    async def icon(bundle_id):
        calls.append(bundle_id)
        return b"synthetic-icon"

    session.icon = icon
    path = asyncio.run(preview.render(session, tmp_path / "preview.html"))
    assert calls == [f"example.a{i}" for i in range(9)]
    doc = Document(path.read_text())
    assert len(list(elements(doc.root, tag="img"))) == 18
    assert [text(n) for n in elements(doc.root, tag="h2")] == ["Draft", "Last read from the phone"]
