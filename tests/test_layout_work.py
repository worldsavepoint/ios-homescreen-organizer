import asyncio

from fake_phone import app, widget
from homescreen import session as transactions, ui
from homescreen.model import Layout
from homescreen.session import Session
from test_integrity import MemorySource


def test_changed_view_computes_one_original_for_all_consumers(tmp_path, monkeypatch):
    s = Session(MemorySource(), tmp_path)
    asyncio.run(s.read())
    s.edit(lambda l: l.swap("a", "b"))
    s.usage = {"ok": True, "days": 7, "opens": {"a": 4}}
    original, calls = s.layout.original, []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(s.layout, "original", counted)
    result = ui.view(s)
    assert result["diff"]["has_changes"] and not result["diff"]["problems"] and result["usage"]["top"]
    assert len(calls) == 1


def test_pin_comparison_describes_each_exported_widget_once_per_side(monkeypatch):
    layout = Layout([[], [widget("Weather", "small"), *[app(str(i), str(i)) for i in range(12)]]])
    observed = layout.original()
    for view in (layout, observed):
        coords, _ = view.place(view.pages[0])
        view.pins = {nid: row * 4 + col for nid, (row, col, _, _) in coords.items()}
    old, widgets = transactions.node_structure, []
    def describe(node):
        if node.kind == "widget":
            widgets.append(node)
        return old(node)
    monkeypatch.setattr(transactions, "node_structure", describe)
    assert transactions._retains_pins(layout, observed)
    assert len(widgets) == 2


def test_locked_edit_encoding_reuses_rollback_state_and_preserves_separate_history(tmp_path, monkeypatch):
    s = Session(MemorySource(), tmp_path)
    asyncio.run(s.read())
    detached = []
    current = Layout.current_snapshot
    def counted(lay):
        detached.append(lay)
        return current(lay)
    monkeypatch.setattr(Layout, "current_snapshot", counted)
    s.edit(lambda l: l.nodes["a"].raw["someFutureKey"]["nested"].clear())
    assert len(detached) == 0
    assert s.undo() and s.require().nodes["a"].raw["someFutureKey"]["nested"] == [1, 2, 3]
    assert s.redo() and s.require().nodes["a"].raw["someFutureKey"]["nested"] == []
