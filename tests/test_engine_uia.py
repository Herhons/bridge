"""Unit tests for Bridge v0.2.1 UIA snapshot, scoped refs, delta, and fail-closed errors."""
from __future__ import annotations

import unittest

from engine.uia import (
    Snapshot,
    UIAError,
    UINode,
    assign_refs,
    pick_unique,
    resolve_ref,
    snapshot_delta,
)


def _make_snapshot(snap_id: str, extra_lines: list[str] | None = None) -> Snapshot:
    children = [
        UINode(role="GroupControl", name="Composer", focusable=True, has_text_pattern=True, rect=(10, 500, 400, 40)),
        UINode(role="ButtonControl", name="发送", rect=(420, 500, 50, 40)),
        UINode(role="TextControl", name="历史消息"),
    ]
    for line in (extra_lines or []):
        children.append(UINode(role="TextControl", name=line))
    root = UINode(role="WindowControl", name="DeepSeek", children=children)
    refs = assign_refs([root], snap_id)
    return Snapshot(snapshot_id=snap_id, hwnd=1001, title="DeepSeek", url="https://chat.deepseek.com/", nodes=[root], refs=refs)


class TestUIAEngine(unittest.TestCase):
    def test_focusable_text_pattern_group_receives_ref(self):
        snap = _make_snapshot("abc12345")
        self.assertIn("@abc12345:e1", snap.refs)
        self.assertEqual(snap.refs["@abc12345:e1"].role, "GroupControl")
        self.assertEqual(snap.refs["@abc12345:e2"].name, "发送")

    def test_resolve_ref_rejects_stale_and_bare_refs(self):
        snap = _make_snapshot("snap0001")
        self.assertEqual(resolve_ref("@snap0001:e2", snap).name, "发送")

        with self.assertRaises(UIAError) as ctx1:
            resolve_ref("@e2", snap)
        self.assertEqual(ctx1.exception.code, "INVALID_ARGS")

        with self.assertRaises(UIAError) as ctx2:
            resolve_ref("@old00000:e2", snap)
        self.assertEqual(ctx2.exception.code, "STALE_REF")

    def test_pick_unique_raises_ambiguous_target(self):
        n1 = UINode(role="ButtonControl", name="发送")
        n2 = UINode(role="ButtonControl", name="发送")
        with self.assertRaises(UIAError) as ctx:
            pick_unique([n1, n2], "send_button")
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_TARGET")

    def test_snapshot_delta_extracts_only_new_reply_lines(self):
        before = _make_snapshot("s1")
        after = _make_snapshot("s2", extra_lines=["第一行回复", "第二行回复"])
        diff = snapshot_delta(before, after)
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["added_lines"], ["第一行回复", "第二行回复"])
        self.assertEqual(diff["added_text"], "第一行回复\n第二行回复")


if __name__ == "__main__":
    unittest.main()
