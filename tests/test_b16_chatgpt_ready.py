"""B16-S2：chatgpt `--new` 之后的「新页可读」闸。

现场（W4f/W4g）：4 席并发时 `--new` 后只 sleep 1.0s，`set_model` 第一句
`read_writer` 读到 unknown 就 `no_writer` 退出；2 席时同样的代码能过。
根因是新会话页的 SPA 重排 + 别的席位切走标签，不是模型菜单本身。

契约：
1. `--ready-wait` 默认 0 —— 为 0 时**保留旧的固定 sleep(1.0)**，单跑行为零变化；
2. >0 时轮询 `page_kind=="new"` 且 writer 读得出，两个条件缺一不可；
3. 一轮等满再点一次新聊天，第二轮仍不行才 fail-closed，绝不猜模式；
4. `no_writer` 要带上读不出的明细（uia/toggle/hint），方便现场分辨原因。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCRIPT = ROOT / "src" / "scripts" / "chatgpt_choose.py"


def load_function(name, namespace):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"), filename=str(SCRIPT))
    definition = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPT), "exec"), namespace)
    return namespace[name]


class Clock:
    """可控时钟：sleep 真的推进时间，循环不会失控。"""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class WaitNewPageTests(unittest.TestCase):
    def _run(self, kinds, writers, *, timeout=5.0, reopen=None):
        clock = Clock()
        kinds = iter(kinds)

        def page_kind(_screen):
            try:
                return next(kinds)
            except StopIteration:
                return "session"

        writer_calls = []

        def read_writer(_screen):
            writer_calls.append(1)
            value = writers[min(len(writer_calls) - 1, len(writers) - 1)]
            return {"writer": value}

        reopen_calls = []

        def open_new_chat(_screen, _site):
            reopen_calls.append(1)
            return reopen if reopen is not None else {"ok": True, "stage": "clicked"}

        namespace = {
            "time": clock,
            "page_kind": page_kind,
            "read_writer": read_writer,
            "_open_new_chat": open_new_chat,
        }
        wait = load_function("_wait_new_page", namespace)
        return wait(object(), object(), timeout), len(writer_calls), len(reopen_calls), clock.t

    def test_ready_on_first_poll(self):
        out, writers, reopens, elapsed = self._run(["new"], ["chat"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["stage"], "new_page_ready")
        self.assertEqual(out["polls"], 1)
        self.assertEqual(out["writer"], "chat")
        self.assertEqual(writers, 1)
        self.assertEqual(reopens, 0)
        self.assertEqual(elapsed, 0.0)

    def test_waits_until_new_page_renders(self):
        out, writers, reopens, _ = self._run(
            ["session", "session", "new"], ["chat"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["polls"], 3)
        # 不是新页时不去读 UIA，省掉一次昂贵的树读
        self.assertEqual(writers, 1)
        self.assertEqual(reopens, 0)

    def test_new_page_but_writer_unknown_is_not_ready(self):
        out, _writers, reopens, _ = self._run(["new"], ["unknown"], timeout=2.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "new_page_not_ready")
        self.assertEqual(reopens, 1)
        self.assertTrue(out["reopen"]["ok"])

    def test_second_round_never_guesses_a_writer(self):
        out, _writers, reopens, _ = self._run(["session"], ["chat"], timeout=1.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "new_page_not_ready")
        self.assertNotIn(out["writer"], ("chat", "work"))
        self.assertIn("hint", out)
        self.assertEqual(reopens, 1)

    def test_failed_reopen_stops_immediately(self):
        out, _writers, reopens, _ = self._run(
            ["session"], ["chat"], timeout=1.0,
            reopen={"ok": False, "stage": "newchat_not_found"})
        self.assertFalse(out["ok"])
        self.assertFalse(out["reopen"]["ok"])
        self.assertEqual(reopens, 1)


class WriterSwitchTests(unittest.TestCase):
    """`--new` 重置成 chat、目标却是 work 档时，必须在新页上切回去。"""

    def _run(self, first_writer, key, *, switch_ok=True):
        calls = []
        writers = iter([first_writer, "work" if switch_ok else first_writer])

        def read_writer(_s):
            try:
                return {"writer": next(writers)}
            except StopIteration:
                return {"writer": "work"}

        namespace = {
            "MODELS": {"work": {"gpt56_luna": ("x", "tpl")},
                       "chat": {"gpt56": ("y", "tpl")}},
            "read_writer": read_writer,
            "choose_writer": lambda s, k: calls.append(k) or {"ok": switch_ok},
            "chip_signature": lambda s: None,
            "enter_models": lambda s, w: {"ok": False, "stage": "no_model_selector"},
        }
        fn = load_function("set_model", namespace)
        return fn(object(), key), calls

    def test_work_tier_on_a_chat_page_switches_writer(self):
        out, calls = self._run("chat", "gpt56_luna")
        self.assertEqual(calls, ["work"])
        self.assertEqual(out["stage"], "no_model_selector")

    def test_same_writer_does_not_switch(self):
        out, calls = self._run("work", "gpt56_luna")
        self.assertEqual(calls, [])
        self.assertEqual(out["stage"], "no_model_selector")

    def test_failed_switch_is_fail_closed(self):
        out, calls = self._run("chat", "gpt56_luna", switch_ok=False)
        self.assertEqual(out["stage"], "writer_switch_failed")
        self.assertEqual(out["want_writer"], "work")


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8-sig")
        cls.config = json.loads((ROOT / "docs" / "bridge_server.json").read_text(
            encoding="utf-8-sig"))

    def test_default_is_zero_and_old_sleep_is_kept(self):
        self.assertIn("--ready-wait", self.text)
        self.assertIn("default=0.0", self.text)
        self.assertIn("else:\n                time.sleep(1.0)", self.text)

    def test_main_calls_the_gate_only_when_enabled(self):
        self.assertIn("if options.ready_wait > 0:", self.text)
        self.assertIn("ready = _wait_new_page(s, _NEWCHAT, options.ready_wait)", self.text)

    def test_no_writer_carries_diagnostics(self):
        self.assertIn('"writer_detail": detail', self.text)
        self.assertIn("state = read_writer(s)", self.text)

    def test_config_enables_a_15_second_gate(self):
        self.assertEqual(self.config["choose_args"]["chatgpt"],
                         ["--new", "--ready-wait", "15"])

    def test_use_timeout_contract_is_untouched(self):
        self.assertEqual(self.config["use_timeout"], 120.0)


if __name__ == "__main__":
    unittest.main()
