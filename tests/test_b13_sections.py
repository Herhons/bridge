"""B13b：真正打开并发的那一下。

四件事各自可测，且都要能证明"不改的时候一个字节不变"：

1. `uilock.sectioned()` 只认环境变量；
2. `_ask_impl` 的发送段（S）与轮询段（R）各自进了短临界区；
3. `ui.wake_enabled()` 默认开、`BRIDGE_UI_WAKE=0` 才关；
4. 服务端在 `endpoint_concurrency > 1` 时才下发三个开关。
"""
import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, server, ui, uilock  # noqa: E402

PROMPT = "请只原样回复：BR13-section-test"


def _node(name, children=()):
    item = ui.Node("TextControl", name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=80, bottom=20)))
    item.children = list(children)
    return item


class SectionedFlagTests(unittest.TestCase):
    def test_default_is_off(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(uilock.SECTIONED_ENV, None)
            self.assertFalse(uilock.sectioned())

    def test_only_exactly_one_enables_it(self):
        with unittest.mock.patch.dict(os.environ, {uilock.SECTIONED_ENV: "1"}):
            self.assertTrue(uilock.sectioned())
        with unittest.mock.patch.dict(os.environ, {uilock.SECTIONED_ENV: "yes"}):
            self.assertFalse(uilock.sectioned())


class WakeFlagTests(unittest.TestCase):
    def test_default_allows_wake(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_UI_WAKE", None)
            self.assertTrue(ui.wake_enabled())

    def test_zero_disables_wake(self):
        with unittest.mock.patch.dict(os.environ, {"BRIDGE_UI_WAKE": "0"}):
            self.assertFalse(ui.wake_enabled())


class _Conv:
    """刚好够跑一次 `_ask_impl` 的假件。"""

    settle = 0
    screen = None

    def __init__(self):
        self.site = "gemini"
        self.writes = 0
        self.submits = 0
        self.n = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        self.n += 1
        root = ui.Node("DocumentControl", "s · App", SimpleNamespace(
            IsEnabled=True, IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=0, top=0, right=600, bottom=400)))
        kids = []
        if self.n >= 3:                      # 发送之后提问气泡才在
            kids.append(_node(PROMPT))
        root.children = kids
        value = PROMPT if self.n == 2 else ""
        return SimpleNamespace(root=root, value=value, url="", busy=False,
                               copies=[], editor=None, sends=())

    def write(self, before, prompt):
        self.writes += 1

    def submit(self, v):
        self.submits += 1

    def clear(self, v):
        pass

    def copy(self, n):
        return ""


class SectionBoundaryTests(unittest.TestCase):
    def test_send_and_poll_each_run_inside_a_section(self):
        seen = []

        class Recorder:
            def __init__(self, owner):
                self.owner = owner

            def __enter__(self):
                seen.append(self.owner)
                return None

            def __exit__(self, *exc):
                return False

        with unittest.mock.patch.object(uilock, "section",
                                        lambda owner="ui", **kw: Recorder(owner)):
            got = conversation.ask(_Conv(), PROMPT, timeout=1.0)
        self.assertIn(f"send:gemini", seen, "发送段必须整段持锁")
        self.assertTrue(any(o.startswith("poll:") for o in seen),
                        f"轮询读取也必须持锁，实测 {seen}")
        self.assertIn(got["stage"], ("timeout", "answered"))


class EnvDownlinkTests(unittest.TestCase):
    def _engine(self, concurrency):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="b13-"))
        (tmp / "src" / "scripts").mkdir(parents=True)
        (tmp / "src" / "scripts" / "grok_use.py").write_text("", encoding="utf-8")
        seen = {}

        def runner(cmd, **kwargs):
            seen.update(kwargs.get("env") or {})
            return {"exit_code": 0, "timeout": False,
                    "stdout": '{"ok":true,"stage":"answered","reply":"x"}'}

        engine = server.Engine(root=tmp, token="t", port=0, runner=runner)
        engine.config["endpoint_concurrency"] = concurrency
        return engine, seen

    def test_concurrent_mode_sends_all_three_switches(self):
        engine, seen = self._engine(4)
        engine._run_use("grok", "p", "test", __import__("time").monotonic() + 30)
        self.assertEqual(seen.get("BRIDGE_UI_SECTIONED"), "1")
        self.assertEqual(seen.get("BRIDGE_UI_WAKE"), "0")
        self.assertEqual(seen.get("BRIDGE_UI_CONCURRENT"), "1")

    def test_serial_mode_sends_none_of_them(self):
        engine, seen = self._engine(1)
        engine._run_use("grok", "p", "test", __import__("time").monotonic() + 30)
        self.assertNotIn("BRIDGE_UI_SECTIONED", seen)
        self.assertNotIn("BRIDGE_UI_CONCURRENT", seen)


if __name__ == "__main__":
    unittest.main()
