"""Tool discovery describes effects and input contracts."""

import asyncio

import pytest

from homescreen import server


@pytest.fixture
def tools(monkeypatch):
    def forbidden_session():
        pytest.fail("tool discovery attempted to create a device session")

    monkeypatch.setattr(server, "session", forbidden_session)
    return {tool.name: tool.model_dump(by_alias=True)
            for tool in asyncio.run(server.mcp.list_tools())}


@pytest.mark.parametrize("name,readonly,destructive,idempotent,openworld", [
    ("read_phone", False, True, False, True),
    ("show_layout", False, False, True, False),
    ("find_apps", False, False, True, False),
    ("icon_colors", False, False, True, True),
    ("get_icons", False, False, True, True),
    ("arrange", False, True, False, False),
    ("move", False, True, False, False),
    ("create_folder", False, True, False, False),
    ("rename_folder", False, True, False, False),
    ("dissolve_folder", False, True, False, False),
    ("file_into_folders", False, True, False, False),
    ("sort_by_color", False, True, False, True),
    ("sort_alphabetically", False, True, False, False),
    ("sort_folder_contents", False, True, False, False),
    ("app_store_categories", False, False, True, True),
    ("place_at", False, True, False, False),
    ("close_gaps", False, True, False, False),
    ("swap", False, True, False, False),
    ("screen_time", False, True, False, True),
    ("optimize_by_usage", False, True, False, True),
    ("page_op", False, True, False, False),
    ("remove_empty", False, True, False, False),
    ("undo", False, True, False, False),
    ("redo", False, True, False, False),
    ("reset_draft", False, True, False, False),
    ("diff", False, False, True, False),
    ("render_preview", False, True, True, True),
    ("apply", False, True, False, True),
    ("list_backups", False, False, True, False),
    ("restore", False, True, False, True),
])
def test_discovery_describes_all_tool_effects(tools, name, readonly, destructive, idempotent, openworld):
    assert tools[name]["annotations"] is not None
    for hint, expected in (("readOnlyHint", readonly), ("destructiveHint", destructive),
                           ("idempotentHint", idempotent), ("openWorldHint", openworld)):
        assert tools[name]["annotations"][hint] is expected


def test_discovery_explains_parameters_and_retains_layout_input_contract(tools):
    for tool in tools.values():
        assert tool["description"].strip()
        for parameter in tool["inputSchema"]["properties"].values():
            assert parameter.get("description", "").strip()
    move = tools["move"]["inputSchema"]
    assert move["required"] == ["item_id", "target"]
    assert move["properties"]["index"]["default"] is None
    assert move["properties"]["before"]["default"] is None
    assert tools["screen_time"]["inputSchema"]["properties"]["days"]["default"] == 30
    assert tools["screen_time"]["inputSchema"]["properties"]["source"]["default"] == "device"
    assert tools["optimize_by_usage"]["inputSchema"]["properties"]["keep_first_page"]["default"] is False
