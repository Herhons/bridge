
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from itertools import chain, repeat
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import browser as b  # noqa: E402


def hwnd_seq(values):
    """第一次调用是 before，之后按 values 走；用完后一直重复最后一个。"""
    return chain(values, repeat(values[-1]))


class SpawnBase(unittest.TestCase):
    def run_spawn(self, seq, *, nonce=None, timeout=1.0, shell_rc=42,
                  proves_nonce=True, place_ok=True, focus_ok=True):
        closed, placed = [], []
        with mock.patch.object(b, "_chrome_hwnds", side_effect=hwnd_seq(seq)), \
                mock.patch.object(b, "shell32") as shell, \
                mock.patch.object(b, "place",
                                  side_effect=lambda h, *a: placed.append(h) if place_ok else (_ for _ in ()).throw(RuntimeError("place failed"))), \
                mock.patch.object(b, "_focus", return_value=focus_ok), \
                mock.patch.object(b, "_wait_ready", return_value=True), \
                mock.patch.object(b, "_spawn_proves_nonce", return_value=proves_nonce), \
                mock.patch.object(b, "_cleanup_spawn",
                                  side_effect=lambda h, v=False: closed.append(h)):
            shell.ShellExecuteW.return_value = shell_rc
            error = None
            try:
                result = b._spawn("https://example.test/app", (0, 0), (800, 600),
                                  timeout, False, nonce=nonce)
            except Exception as exc:       # noqa: BLE001 - 有些失败会往上抛
                result, error = None, exc
        return result, {"closed": closed, "placed": placed, "error": error}


class NoLateAdoptionTests(SpawnBase):
    def test_timeout_never_rechecks_the_window_list(self):
        """超时后**不许**再"看一眼有没有迟到窗口"——那一步没有任何归属证据。

        我们只能断言"没有再调用一次枚举"：before 1 次，循环 0 次（timeout=0）。
        """
        with mock.patch.object(b, "_chrome_hwnds", return_value={1}) as hwnds, \
                mock.patch.object(b, "shell32") as shell:
            shell.ShellExecuteW.return_value = 42
            result = b._spawn("u", (0, 0), (1, 1), 0.0, False)
        self.assertIsNone(result)
        self.assertEqual(hwnds.call_count, 1,
                         "超时后不该再看一次窗口列表（旧代码会，然后关掉它）")

    def test_late_window_is_never_closed(self):
        """核心回归：超时返回时，一个窗口都不许关。"""
        result, calls = self.run_spawn([{1}], timeout=0.0)
        self.assertIsNone(result)
        self.assertEqual(calls["closed"], [])

    def test_source_has_no_late_adoption_left(self):
        source = (ROOT / "src" / "qi" / "browser.py").read_text(encoding="utf-8")
        self.assertNotIn("late = _chrome_hwnds()", source)
        self.assertNotIn("late", source.split("def _spawn")[1].split("def ensure_site")[0],
                         "_spawn 里不该再有迟到窗口的痕迹")


class OwnershipTests(SpawnBase):
    def test_single_new_window_without_nonce_is_adopted(self):
        result, calls = self.run_spawn([{1}, {1, 2}])
        self.assertEqual(result, 2)
        self.assertEqual(calls["closed"], [], "认领成功不该关它")

    def test_multiple_new_windows_are_never_adopted_nor_closed(self):
        result, calls = self.run_spawn([{1}, {1, 2, 3}])
        self.assertIsNone(result)
        self.assertEqual(calls["closed"], [], "归属不明时一个都不许关")

    def test_shell_launch_failure_returns_none(self):
        result, calls = self.run_spawn([{1}], shell_rc=0)
        self.assertIsNone(result)
        self.assertEqual(calls["closed"], [])

    def test_place_failure_closes_the_proven_window(self):
        """归属**已证**（nonce 命中）、但后续步骤失败 -> 允许关（那是我们自己的窗口）。"""
        result, calls = self.run_spawn([{1}, {1, 2}], nonce="abc123", place_ok=False)
        self.assertIsNone(result)
        self.assertIsInstance(calls["error"], RuntimeError)
        self.assertEqual(calls["closed"], [2],
                         "归属已证的窗口在后续失败时必须收尾，别泄漏")

    def test_place_failure_keeps_the_weak_evidence_window(self):
        """F2（独立复核 2026-11-07）：没给 nonce 时只是弱证据，失败**不许**关。

        复核实测（repro_a A4）：等待期恰有一个新窗口被认领，`_focus` 失败 → 旧代码
        把它关掉。施工单只允许关"持有本次启动归属证据"的窗口。
        """
        result, calls = self.run_spawn([{1}, {1, 2}], focus_ok=False)
        self.assertIsNone(result)
        self.assertEqual(calls["closed"], [],
                         "弱证据（恰好多 1 个窗口）不足以杀掉一个窗口")


class NonceTests(SpawnBase):
    def test_nonce_hit_adopts_the_window(self):
        result, calls = self.run_spawn([{1}, {1, 2}], nonce="abc123")
        self.assertEqual(result, 2)

    def test_nonce_miss_keeps_the_window_open(self):
        """有窗口但读不到 nonce：归属不成立，**保留不关**（可能是别人开的）。"""
        result, calls = self.run_spawn([{1}, {1, 2}], nonce="abc123",
                                       proves_nonce=False)
        self.assertIsNone(result)
        self.assertEqual(calls["closed"], [], "无归属证据的窗口一律保留")

    def test_with_nonce_preserves_query_and_fragment(self):
        self.assertEqual(b._with_nonce("https://x.test/a", "n1"),
                         "https://x.test/a?bridge=n1")
        self.assertIn("k=1", b._with_nonce("https://x.test/a?k=1", "n1"))
        self.assertIn("bridge=n1", b._with_nonce("https://x.test/a?k=1", "n1"))
        self.assertTrue(b._with_nonce("https://x.test/a#f", "n1").endswith("#f"))

    def test_nonce_only_for_verified_sites(self):
        self.assertIn("chatgpt", b.SPAWN_NONCE_SITES)
        self.assertIn("gemini", b.SPAWN_NONCE_SITES)
        self.assertIn("grok", b.SPAWN_NONCE_SITES)
        self.assertNotIn("deepseek", b.SPAWN_NONCE_SITES,
                         "deepseek 未验证是否保留 query，先不启用 P1")

    def test_spawn_nonce_generates_hex_for_whitelisted_sites(self):
        value = b._spawn_nonce("grok")
        self.assertIsInstance(value, str)
        self.assertEqual(len(value), 16)
        int(value, 16)                       # 必须是合法 hex
        self.assertIsNone(b._spawn_nonce("deepseek"))

    def test_nonce_differs_each_call(self):
        self.assertNotEqual(b._spawn_nonce("grok"), b._spawn_nonce("grok"))


class KnownGapTests(unittest.TestCase):
    def test_t1_2_is_declared_as_not_implemented(self):
        doc = (ROOT / "docs" / "known_gaps.md").read_text(encoding="utf-8")
        self.assertIn("T1-2", doc)
        self.assertIn("NOT_IMPLEMENTED", doc)
        self.assertIn("全局低级键盘/鼠标钩子", doc)
        self.assertNotIn("T1-2 | pending", doc)

    def test_guard_code_is_kept_not_removed(self):
        """跳过实现 ≠ 删代码。现有 GetLastInputInfo 机制必须原样在。"""
        source = (ROOT / "src" / "qi" / "guard.py").read_text(encoding="utf-8")
        self.assertIn("GetLastInputInfo", source)
        self.assertIn("SYNTHETIC_TTL", source)
        self.assertIn("def user_moved_since", source)


if __name__ == "__main__":
    unittest.main()
