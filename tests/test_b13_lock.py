"""B13：锁从"整轮"改成"分段"。

用户要的是 4 席并发。障碍是：`uilock.run(name, main)` 把**整个 main（含 900 秒等待）**
包在全局锁里，于是 4 个端只能串行 —— 这正是 `CALIBER §3.1` 明令禁止的
「持有全局锁去串行等待完整回答」。

改法有两半，这里各测一半：
1. `entry()`：入口**不再持外层锁**，只保留"拿不到锁 → JSON + 退出码 8"的语义；
2. `section()` / `turn()`：短临界区与同端回合锁，互斥仍然成立。
"""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import exit_codes, uilock  # noqa: E402


class EntryTests(unittest.TestCase):
    def _with_holder(self, action):
        started = threading.Event()
        release = threading.Event()

        def holder():
            with uilock.hold("holder", timeout=5):
                started.set()
                # 持锁到探针跑完为止。原来只等 2s：机器一忙、探针线程晚了 2s，
                # 锁已经放掉，探针就"成功"拿到锁 —— 这条用例会假红（克隆环境实测）。
                release.wait(20)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(10))
        try:
            return action()
        finally:
            release.set()
            thread.join(10)

    def test_default_mode_still_takes_the_whole_run_lock(self):
        """默认（未开分段模式）与改动前一致：别人持锁时进不去，返回 8。"""
        import os
        from unittest import mock
        # 默认等 60 秒：给个短超时，否则会一直等到 holder 放锁（那不是失败）
        with mock.patch.dict(os.environ, {"BRIDGE_UI_LOCK_TIMEOUT": "0.1"}):
            code = self._with_holder(lambda: uilock.entry("probe", lambda: 0))
        self.assertEqual(code, exit_codes.UI_LOCK_BUSY)

    def test_sectioned_mode_does_not_hold_the_run_lock(self):
        """`BRIDGE_UI_SECTIONED=1`：等回答期间不持锁，别人持锁也能跑 body。"""
        import os
        from unittest import mock
        ran = []
        with mock.patch.dict(os.environ, {uilock.SECTIONED_ENV: "1"}):
            code = self._with_holder(
                lambda: uilock.entry("probe", lambda: ran.append(True) or 0))
        self.assertEqual(code, 0)
        self.assertEqual(ran, [True], "分段模式下不该因为别人持锁就进不去")

    def test_entry_turns_busy_into_exit_8(self):
        def boom():
            raise uilock.Busy("ui lock busy: owner=someone")
        self.assertEqual(uilock.entry("probe", boom), exit_codes.UI_LOCK_BUSY)
        self.assertEqual(exit_codes.UI_LOCK_BUSY, 8)

    def test_entry_returns_the_body_exit_code(self):
        self.assertEqual(uilock.entry("probe", lambda: 5), 5)


class SectionTests(unittest.TestCase):
    def test_section_still_excludes_other_threads(self):
        """分段锁不能变成"谁都能进"—— 桌面动作必须互斥。"""
        started = threading.Event()
        release = threading.Event()
        seen = {}

        def holder():
            with uilock.section("t1", timeout=5):
                started.set()
                release.wait(2)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        try:
            with self.assertRaises(uilock.Busy):
                with uilock.section("t2", timeout=0.1):
                    seen["in"] = True
        finally:
            release.set()
            thread.join(2)
        self.assertNotIn("in", seen)

    def test_section_is_reentrant_in_the_same_thread(self):
        with uilock.section("outer", timeout=2):
            with uilock.section("inner", timeout=0.2):
                pass

    def test_section_releases_on_exception(self):
        with self.assertRaises(RuntimeError):
            with uilock.section("boom", timeout=2):
                raise RuntimeError("boom")
        with uilock.section("after", timeout=0.5):
            pass


class TurnTests(unittest.TestCase):
    def setUp(self):
        """把锁文件根目录挪到临时目录。

        不隔离的话，`runs/turn/*.lock`、`runs/ui.lock` 是**全局**文件：上一条用例
        或别的进程留下的锁会让 holder 线程卡在 `turn(timeout=5)` 里，于是
        `started.wait(2)` 偶发失败。2026-11-12 就是这样误拦了一次推送
        （`test_same_endpoint_excludes_but_does_not_block_the_ui_lock`，
        `AssertionError: False is not true`，日志开头是 `[lock] UI busy: owner=…`）。
        """
        import shutil
        import tempfile
        from unittest.mock import patch

        self._tmp = tempfile.mkdtemp(prefix="b13-lock-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        patcher = patch.object(uilock, "ROOT", Path(self._tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_turn_uses_its_own_file(self):
        self.assertEqual(uilock.lock_path(name="gemini").name, "gemini.lock")
        self.assertNotEqual(uilock.lock_path(name="gemini"), uilock.lock_path())
        self.assertEqual(uilock.lock_path(name="gemini").parent.name, "turn")

    def test_same_endpoint_excludes_but_does_not_block_the_ui_lock(self):
        """同端互斥（跨线程），但**不**挡住别的端拿桌面锁 —— 两个 path 可嵌套。

        同线程再来一次是**重入放行**（`hold` 按 path 判重），那是既有的正确语义，
        不是"没互斥"；所以要验互斥必须换一个线程。
        """
        started = threading.Event()
        release = threading.Event()

        def holder():
            with uilock.turn("gemini", timeout=5):
                started.set()
                release.wait(2)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        try:
            with uilock.section("other-endpoint", timeout=1.0):
                pass          # 别的端仍能进桌面段（不同 path）
            with self.assertRaises(uilock.Busy):
                with uilock.turn("gemini", timeout=0.1):
                    pass      # 同一个端再来一份 → 挡住
        finally:
            release.set()
            thread.join(2)

    def test_turn_releases_after_use(self):
        with uilock.turn("grok", timeout=2):
            pass
        with uilock.turn("grok", timeout=0.5):
            pass


if __name__ == "__main__":
    unittest.main()
