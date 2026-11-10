"""T2-1 接线测试：purpose 必须真的从每个 use 入口传到 conversation.ask。

**为什么需要这个文件**：B2 的 25 条测试全部直接构造 `_Ledger` 实例测它的行为，
没有一条测"入口有没有把 purpose 传下来"。结果 codex / dsh / opencode 三个入口
的调用点漏了 `purpose=purpose`，账本压根没启用，测试却全绿。

这是典型的"测了零件、没测接线"。本文件专门盯接线。

2026-11-07 由 opencode 独立复核（TASK OC-AUDIT-20261107-1300-k7q2）之后补：
复核方与施工方各自独立发现了不同的问题，本文件是施工方自查那一条的防复发。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "scripts"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def missing_purpose_lines(source: str) -> list[int]:
    """返回所有 `send_and_receive(...)` 调用里**没带 purpose=** 的行号。"""
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "send_and_receive":
            continue
        if not any(kw.arg == "purpose" for kw in node.keywords):
            bad.append(node.lineno)
    return bad


class WiringTest(unittest.TestCase):
    def test_detector_is_not_vacuous(self):
        """反例自检：没有 purpose 的源码必须被报出来，否则这个检查本身是摆设。"""
        fake = (
            "def f():\n"
            "    return send_and_receive(conv, prompt, timeout,\n"
            "                            pre_send=door)\n"
        )
        self.assertEqual(missing_purpose_lines(fake), [2])

    def test_detector_accepts_forwarded_purpose(self):
        good = (
            "def f():\n"
            "    return send_and_receive(conv, prompt, timeout,\n"
            "                            pre_send=door, purpose=purpose)\n"
        )
        self.assertEqual(missing_purpose_lines(good), [])

    def test_every_sending_use_script_forwards_purpose(self):
        """每个会发消息的 *_use.py，都必须把 purpose 透传下去。"""
        offenders = {}
        for path in sorted(SCRIPTS.glob("*_use.py")):
            source = path.read_text(encoding="utf-8")
            if "send_and_receive(" not in source:
                continue
            bad = missing_purpose_lines(source)
            if bad:
                offenders[path.name] = bad
        self.assertEqual(offenders, {},
                         "这些入口调用 conversation.ask 时没传 purpose，"
                         "提交账本对它们不生效：" + repr(offenders))

    def test_flow_forwards_purpose(self):
        """flow.ask 是 chatgpt / deepseek 的公共入口，同样不许漏。"""
        source = (ROOT / "src" / "qi" / "flow.py").read_text(encoding="utf-8")
        self.assertEqual(missing_purpose_lines(source), [])


class LedgerConstructionTest(unittest.TestCase):
    """`ask()` 必须把 purpose 转进 `_Ledger`；不给 purpose 时必须退化成老行为。"""

    def _run(self, purpose):
        from qi import conversation as conv_mod

        seen = {}

        class Recorder:
            def __init__(self, conversation, prompt, purpose_):
                seen["purpose"] = purpose_
                self.enabled = bool(purpose_)
                self.root = None
                self.scope = None

            def reserve(self, written):
                return None

            def finish(self, result):
                seen["finished"] = True

            def fail_on_exception(self):
                pass

        class FakeConv:
            site = "opencode"

        real_ledger = conv_mod._Ledger
        real_impl = conv_mod._ask_impl
        conv_mod._Ledger = Recorder
        conv_mod._ask_impl = lambda *a, **kw: {"ok": True, "stage": "answered",
                                               "ledger_seen": kw.get("ledger") is not None}
        try:
            result = conv_mod.ask(FakeConv(), "prompt", timeout=1.0, purpose=purpose)
        finally:
            conv_mod._Ledger = real_ledger
            conv_mod._ask_impl = real_impl
        return seen, result

    def test_purpose_is_forwarded_to_ledger(self):
        seen, result = self._run("test")
        self.assertEqual(seen.get("purpose"), "test")
        self.assertTrue(result.get("ledger_seen"), "给了 purpose，_ask_impl 必须收到 ledger")
        self.assertTrue(seen.get("finished"))

    def test_no_purpose_keeps_old_behaviour(self):
        seen, result = self._run(None)
        self.assertIsNone(seen.get("purpose"))
        self.assertFalse(result.get("ledger_seen"), "没给 purpose 时不得记账（老行为不变）")


if __name__ == "__main__":
    unittest.main()
