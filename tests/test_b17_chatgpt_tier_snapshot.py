"""B17：chatgpt 发送前的档位快照（新界面没有只读档位文本）。

2026-11-09 现场：S2b 里 chatgpt 的 choose 明明成功（`read=uia`），use 却
`stage=tier_unconfirmed / reason=tier_unreadable`（退出码 5）——因为
`sendgate.pre_send` 的复核读的是芯片文本，而新界面那个 Button 只剩
「选择 ChatGPT 模型」一个名字，没有子控件、没有 Value/HelpText。

契约：
1. 读数挪到**写入之前**（新对话开好、草稿清掉之后），值通过 `reread` 交给闸门；
   `snapshot_read` 自己**绝不动 UI**（动 UI 会把 last_input 推开 →
   user_took_over）；
2. 快照缺失 / 读失败 / 过期 / 缺维度 → `value=None`，闸门照旧 fail-closed
   （tier_unreadable，退出码 5），**不许**拿回执里的目标值顶替；
3. 默认路径不变：没给 `reread` 时 `pre_send` 仍然走 `chip.read`。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

USE = ROOT / "src" / "scripts" / "chatgpt_use.py"


def load(names, namespace):
    tree = ast.parse(USE.read_text(encoding="utf-8-sig"), filename=str(USE))
    wanted = [node for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(USE), "exec"), namespace)
    return namespace


def namespace(**overrides):
    """装载快照三件套，返回可改的 namespace（`_SNAPSHOT` 是共享的那个 dict）。"""
    class _Section:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    fake_uilock = types.SimpleNamespace(section=lambda *a, **k: _Section())
    ns = {"time": time, "os": __import__("os"), "sys": sys, "uilock": fake_uilock,
          "SNAPSHOT_MAX_AGE": 120.0, "_SNAPSHOT": {}}
    ns.update(overrides)
    load({"take_snapshot", "snapshot_read", "prepare"}, ns)
    return ns


class TakeSnapshotTests(unittest.TestCase):
    def _choose(self, writer="work", model="gpt56_luna", effort="low"):
        mod = types.SimpleNamespace()
        mod.EFFORTS = {"chat": {}, "work": {}}
        mod.read_writer = lambda screen: {"writer": writer, "source": "uia_toggle"}
        mod.read_model = lambda screen, w: {"model": model, "source": "uia"}
        mod.read_effort = lambda screen, w: {"effort": effort, "source": "uia_slider"}
        return mod

    def test_records_writer_model_effort_and_sources(self):
        ns = namespace()
        with mock.patch.dict(sys.modules, {"chatgpt_choose": self._choose()}):
            snap = ns["take_snapshot"](object())
        self.assertEqual(snap["writer"], "work")
        self.assertEqual(snap["model"], "gpt56_luna")
        self.assertEqual(snap["effort"], "low")
        self.assertEqual(snap["effort_source"], "uia_slider")
        self.assertNotIn("error", snap)

    def test_unknown_writer_is_an_error_not_a_guess(self):
        ns = namespace()
        with mock.patch.dict(sys.modules, {"chatgpt_choose": self._choose(writer="unknown")}):
            snap = ns["take_snapshot"](object())
        self.assertEqual(snap["error"], "writer_unknown")
        self.assertNotIn("model", snap)

    def test_exception_is_swallowed_and_recorded(self):
        mod = self._choose()

        def boom(*_a, **_k):
            raise RuntimeError("UIA 挂了")

        mod.read_model = boom
        ns = namespace()
        with mock.patch.dict(sys.modules, {"chatgpt_choose": mod}):
            snap = ns["take_snapshot"](object())
        self.assertIn("RuntimeError", snap["error"])


class SnapshotReadTests(unittest.TestCase):
    def test_fresh_snapshot_reads_back_both_dimensions(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": "gpt56_luna",
                                "effort": "low", "at": time.monotonic()})
        got = ns["snapshot_read"]("composite")
        self.assertEqual(got["value"], {"model": "gpt56_luna", "effort": "low"})
        self.assertEqual(got["writer"], "work")
        self.assertEqual(got["source"], "use_snapshot")

    def test_single_dimension_returns_plain_value(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": "gpt56_luna",
                                "effort": "low", "at": time.monotonic()})
        self.assertEqual(ns["snapshot_read"]("effort")["value"], "low")

    def test_missing_snapshot_is_unreadable(self):
        got = namespace()["snapshot_read"]("composite")
        self.assertIsNone(got["value"])
        self.assertEqual(got["reason"], "snapshot_missing")

    def test_failed_snapshot_is_unreadable(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"error": "writer_unknown", "at": time.monotonic()})
        got = ns["snapshot_read"]("composite")
        self.assertIsNone(got["value"])
        self.assertIn("snapshot_failed", got["reason"])

    def test_stale_snapshot_is_unreadable(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": "gpt56_luna",
                                "effort": "low", "at": time.monotonic() - 121})
        got = ns["snapshot_read"]("composite")
        self.assertIsNone(got["value"])
        self.assertIn("snapshot_stale", got["reason"])

    def test_incomplete_snapshot_does_not_borrow_receipt_values(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": None,
                                "effort": "low", "at": time.monotonic()})
        got = ns["snapshot_read"]("composite")
        self.assertIsNone(got["value"])
        self.assertEqual(got["reason"], "snapshot_incomplete")
        self.assertEqual(got["missing"], ["model"])

    def test_prepare_never_fails_the_ask(self):
        mod = types.SimpleNamespace()
        mod.EFFORTS = {"chat": {}, "work": {}}
        mod.read_writer = lambda screen: {"writer": "unknown"}
        ns = namespace()
        with mock.patch.dict(sys.modules, {"chatgpt_choose": mod}):
            out = ns["prepare"](object())
        self.assertTrue(out["ok"])


class GateIntegrationTests(unittest.TestCase):
    """`_composite_reread` 是闸门真正消费快照的地方。"""

    def _receipt(self):
        return {"writer": "work",
                "dimensions": {"model": {"readback": "gpt56_luna"},
                               "effort": {"readback": "low"}}}

    def _check(self, ns):
        from qi import tier_guard
        return tier_guard._composite_reread(
            self._receipt(), lambda dimension: ns["snapshot_read"](dimension))

    def test_matching_snapshot_confirms(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": "gpt56_luna",
                                "effort": "low", "at": time.monotonic()})
        out, failure = self._check(ns)
        self.assertIsNone(failure)
        self.assertTrue(out["checked"])
        self.assertEqual(out["source"], "use_snapshot")

    def test_drift_is_reported_as_drift(self):
        ns = namespace()
        ns["_SNAPSHOT"].update({"writer": "work", "model": "gpt56_sol",
                                "effort": "low", "at": time.monotonic()})
        _out, failure = self._check(ns)
        self.assertEqual(failure.reason, "tier_drift")

    def test_missing_snapshot_refuses_to_send(self):
        _out, failure = self._check(namespace())
        self.assertEqual(failure.reason, "tier_unreadable")
        self.assertEqual(failure.code, 5)


class PreSendWiringTests(unittest.TestCase):
    def test_given_reread_replaces_chip_read(self):
        from qi import sendgate
        seen = {}

        def fake_gate(*args, **kwargs):
            seen["reread"] = kwargs.get("reread")
            return {"ok": True}

        def boom(*_a, **_k):
            raise AssertionError("不该走默认的 chip.read")

        sentinel = lambda dimension: {"value": "x"}          # noqa: E731
        screen = types.SimpleNamespace(target_hwnd=None)
        with mock.patch.object(sendgate.human, "raise_if_hit", return_value=None), \
                mock.patch.object(sendgate, "page_stamp", return_value={}), \
                mock.patch.object(sendgate.chip, "read", side_effect=boom), \
                mock.patch.object(sendgate.tier_guard, "gate", side_effect=fake_gate):
            sendgate.pre_send(screen, "chatgpt", "test", root=object(), reread=sentinel)
        self.assertIs(seen["reread"], sentinel)

    def test_default_still_uses_chip_read(self):
        from qi import sendgate
        seen = {}

        def fake_gate(*args, **kwargs):
            seen["reread"] = kwargs.get("reread")
            return {"ok": True}

        screen = types.SimpleNamespace(target_hwnd=None)
        with mock.patch.object(sendgate.human, "raise_if_hit", return_value=None), \
                mock.patch.object(sendgate, "page_stamp", return_value={}), \
                mock.patch.object(sendgate.chip, "read",
                                  return_value={"value": "gpt56_luna"}) as rd, \
                mock.patch.object(sendgate.tier_guard, "gate", side_effect=fake_gate):
            sendgate.pre_send(screen, "chatgpt", "test", root=object())
            self.assertTrue(callable(seen["reread"]))
            seen["reread"]("composite")
        self.assertTrue(rd.called)


class BlankNewSkipTests(unittest.TestCase):
    """B17：`--new` 不能把 choose 刚设好的模型再打回默认。"""

    def _site(self, predicate):
        from qi import flow
        return flow.Site(name="chatgpt", title_hint="Google Chrome", composer="c",
                         send="s", actions=("a",), copy="cp",
                         composer_offset=(0, 0), newchat=("n",), blank_new=predicate)

    def test_blank_new_page_skips_the_click(self):
        from qi import flow
        out = flow._open_new_chat(object(), self._site(lambda screen: True))
        self.assertEqual(out["stage"], "already_new")

    def test_false_predicate_falls_through_to_clicking(self):
        from qi import flow
        screen = types.SimpleNamespace(target_hwnd=None)
        with mock.patch.object(flow.ui, "document", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                flow._open_new_chat(screen, self._site(lambda screen: False))

    def test_broken_predicate_does_not_crash(self):
        from qi import flow
        screen = types.SimpleNamespace(target_hwnd=None)

        def boom(_screen):
            raise RuntimeError("读不出来")

        with mock.patch.object(flow.ui, "document", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                flow._open_new_chat(screen, self._site(boom))


class BlankNewPredicateTests(unittest.TestCase):
    def _fn(self, page_kind):
        mod = types.SimpleNamespace(page_kind=page_kind)
        ns = {"sys": sys, "os": __import__("os")}
        load({"_blank_new"}, ns)
        with mock.patch.dict(sys.modules, {"chatgpt_choose": mod}):
            return ns["_blank_new"](object())

    def test_new_chat_page_is_blank(self):
        self.assertTrue(self._fn(lambda screen: "new"))

    def test_session_page_is_not_blank(self):
        self.assertFalse(self._fn(lambda screen: "session"))

    def test_unreadable_page_kind_falls_back_to_clicking(self):
        def boom(_screen):
            raise RuntimeError("树读不到")

        self.assertFalse(self._fn(boom))


class LocalChatPersistedTests(unittest.TestCase):
    """B17：临时会话落库不算换会话；其他路径变化照旧拒。"""

    def test_local_to_real_is_the_same_conversation(self):
        from qi import conversation as conv
        self.assertTrue(conv._local_chat_persisted(
            {"path": "/c/local-chatgpt:87532ca8-de82"}, {"path": "/c/6af1ca10-0c3c"}))

    def test_other_path_changes_are_still_refused(self):
        from qi import conversation as conv
        self.assertFalse(conv._local_chat_persisted({"path": "/c/aaa"}, {"path": "/c/bbb"}))
        self.assertFalse(conv._local_chat_persisted({"path": "/"}, {"path": "/c/bbb"}))
        self.assertFalse(conv._local_chat_persisted({}, {"path": "/c/bbb"}))
        self.assertFalse(conv._local_chat_persisted({"path": "/c/local-x"}, {}))


if __name__ == "__main__":
    unittest.main()
