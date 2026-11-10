"""CARD-06a：档位回执 + UIA 文档丢失唤醒 + 最小化窗口 + 退出码登记。

全部离线：不开窗口、不点桌面、不发消息、不连 COM。
被点名的九类场景逐条对应：

    回执各分支（缺失 / schema / 端不符 / 目标≠读回 / 已消费 / 正常）
    篡改（target 与 readback 对不上、JSON 被改成别的端）
    过期 / 未来时间
    并发消费（同进程 8 线程 + 跨进程 4 个真进程，只有 1 个拿到）
    发送失败作废（消费之后就是废的，必须重新 choose）
    UI 档位被手动改（drift：读到 expert，回执写的是 fast）
    唤醒重试计数与超时（ui.candidates 注入 read/sleep/clock；3 轮上限、总限时）
    窗口恢复失败（window.restore 注入假 Win32；不看 ShowWindow 返回值）
    退出码表（0..7、stage 映射、与 docs/exit-codes.md 逐行对齐、5/6/7 未被占用）
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import argparse
import ast
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SRC / "scripts") not in sys.path:
    sys.path.insert(0, str(SRC / "scripts"))

from qi import exit_codes as ec          # noqa: E402
from qi import tier_guard as tg         # noqa: E402
from qi import ui                       # noqa: E402
from qi import window as window_mod     # noqa: E402

TIERS = ROOT / "docs" / "tiers.json"
DOC = ROOT / "docs" / "exit-codes.md"


# ---------------------------------------------------------------- 小工具
def load_function(path: Path, name: str, namespace: dict):
    """把单个函数从源码里抠出来 exec（和 test_web_cli 同一套手法）。

    这样测的是**真函数**，只是没有包上下文——所以被测代码里对可选依赖要用
    `globals().get(...)`，取不到就跳过。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def receipt(self, endpoint="grok", **kwargs) -> dict:
        """写一张回执到**本用例的临时目录**（绝不碰真的 runs\\tier）。"""
        kwargs.setdefault("root", self.tmp)
        kwargs.setdefault("target", "fast")
        kwargs.setdefault("readback", kwargs["target"])
        return tg.write_receipt(endpoint, **kwargs)


# ---------------------------------------------------------------- 1. 回执各分支
class ReceiptWriteTests(Base):
    def test_receipt_has_every_required_field(self):
        payload = self.receipt(hwnd=None, tab=3, raw={"after": "fast"})
        for key in ("schema_version", "endpoint", "target", "readback", "hwnd", "pid",
                    "process_start_time", "tab", "created_at", "created_monotonic",
                    "monotonic_origin", "raw"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], tg.SCHEMA_VERSION)
        self.assertEqual(payload["tab"], 3)
        self.assertIsInstance(payload["created_monotonic"], float)
        self.assertIsInstance(payload["monotonic_origin"], float)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.receipt()
        left = list((self.tmp / "runs" / "tier").glob("*.tmp"))
        self.assertEqual(left, [], f"临时文件没清干净：{left}")
        self.assertTrue((self.tmp / "runs" / "tier" / "grok.json").exists())

    def test_created_at_is_utc_iso(self):
        payload = self.receipt()
        self.assertTrue(payload["created_at"].endswith("+00:00"),
                        payload["created_at"])
        self.assertIsNotNone(tg.parse_utc(payload["created_at"]))

    def test_endpoint_name_must_be_safe(self):
        with self.assertRaises(ValueError):
            tg.receipt_path("../evil")

    def test_record_choice_never_raises(self):
        """写回执失败不许把 choose 带崩。"""
        out = tg.record_choice("../evil", target="x", readback="x")
        self.assertFalse(out["written"])
        self.assertIn("error", out["reason"])

    def test_record_choice_reports_mismatch(self):
        out = tg.record_choice("grok", target="fast", readback=None, root=self.tmp)
        self.assertTrue(out["written"])
        self.assertEqual(out["reason"], "readback_mismatch")


# ---------------------------------------------------------------- 2. 校验各分支
class ReceiptCheckTests(Base):
    def reason(self, *, endpoint="grok", **kwargs):
        # 这些用例考的是"回执本身成不成立"，不是"UI 读不读得回来"。
        # 06b 起 gate 默认要求读回（fail-closed），这里显式关掉：
        # 读回相关的用例在 test_card06b.py 里单独覆盖。
        kwargs.setdefault("require_readback", False)
        try:
            tg.gate(endpoint, kwargs.pop("purpose", "test"), root=self.tmp,
                    do_consume=False, **kwargs)
        except tg.TierUnconfirmed as exc:
            return exc.reason
        return "ok"

    def test_missing_receipt(self):
        self.assertEqual(self.reason(), "receipt_missing")

    def test_schema_version_mismatch(self):
        path = tg.receipt_path("grok", root=self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 99, "endpoint": "grok"}),
                        encoding="utf-8")
        self.assertEqual(self.reason(), "schema_mismatch")

    def test_endpoint_mismatch_is_tamper(self):
        path = tg.receipt_path("grok", root=self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "endpoint": "gemini",
                                    "target": "fast", "readback": "fast"}),
                        encoding="utf-8")
        self.assertEqual(self.reason(), "endpoint_mismatch")

    def test_broken_json_counts_as_missing(self):
        path = tg.receipt_path("grok", root=self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.reason(), "receipt_missing")

    def test_target_and_readback_must_agree(self):
        """篡改：目标写 expert、读回写 fast —— 只改一个字段就想骗过去。"""
        self.receipt(target="expert", readback="fast")
        self.assertEqual(self.reason(purpose="review"), "readback_mismatch")

    def test_readback_null_is_rejected(self):
        self.receipt(target="fast", readback=None)
        self.assertEqual(self.reason(), "readback_missing")

    def test_expired_receipt(self):
        self.receipt(now="2020-01-01T00:00:00+00:00")
        self.assertEqual(self.reason(), "expired")

    def test_ttl_is_two_minutes(self):
        self.assertEqual(tg.TTL_SECONDS, 120.0)

    def test_future_timestamp(self):
        self.receipt(now="2099-01-01T00:00:00+00:00")
        self.assertEqual(self.reason(), "future_timestamp")

    def test_tiny_clock_skew_is_tolerated(self):
        from datetime import datetime, timedelta, timezone
        skewed = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.receipt(now=skewed)
        self.assertEqual(self.reason(), "ok")

    def test_age_just_inside_ttl_still_passes(self):
        from datetime import datetime, timedelta, timezone
        edge = (datetime.now(timezone.utc) - timedelta(seconds=118)).isoformat()
        self.receipt(now=edge)
        self.assertEqual(self.reason(), "ok")

    def test_consumed_receipt_is_rejected(self):
        self.receipt()
        self.assertTrue(tg.consume("grok", root=self.tmp)["ok"])
        self.assertEqual(self.reason(), "consumed")

    def test_purpose_test_requires_the_lowest_tier(self):
        self.receipt(target="expert", readback="expert")
        self.assertEqual(self.reason(purpose="test"), "purpose_mismatch")
        self.assertEqual(self.reason(purpose="review"), "ok")

    def test_purpose_accept_requires_the_highest_tier(self):
        self.receipt(target="fast", readback="fast")
        self.assertEqual(self.reason(purpose="accept"), "purpose_mismatch")

    def test_hwnd_mismatch(self):
        self.receipt(hwnd=None)
        self.assertEqual(self.reason(live={"hwnd": 999, "pid": 1,
                                          "process_start_time": 2}),
                         "receipt_identity")

    def test_pid_mismatch_after_client_restart(self):
        self.receipt(hwnd=None)
        live = {"hwnd": None, "pid": 4242, "process_start_time": None}
        self.assertEqual(self.reason(live=live), "receipt_identity")

    def test_window_facts_read_the_real_window(self):
        fact = tg.fingerprint(None)
        self.assertEqual(fact, {"hwnd": None, "pid": None, "process_start_time": None})

    def test_unknown_endpoint_says_so(self):
        self.receipt(endpoint="nope")
        self.assertEqual(self.reason(endpoint="nope"), "endpoint_not_in_table")

    def test_failure_carries_actionable_hint_and_code(self):
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, do_consume=False,
                    require_readback=False)
        exc = caught.exception
        self.assertEqual(exc.code, 5)
        self.assertEqual(exc.stage, "tier_unconfirmed")
        self.assertIn("grok_choose", exc.hint)
        self.assertEqual(exc.as_result()["stage"], "tier_unconfirmed")


# ---------------------------------------------------------------- 3. 消费 / 并发
class ConsumeTests(Base):
    def test_gate_consumes_before_returning(self):
        self.receipt()
        self.assertTrue(tg.gate("grok", "test", root=self.tmp,
                                require_readback=False)["ok"])
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"])

    def test_send_failure_still_burns_the_receipt(self):
        """发送失败也作废：gate 已经消费，第二次必须重新 choose。"""
        self.receipt()
        tg.gate("grok", "test", root=self.tmp, require_readback=False)
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, require_readback=False)
        self.assertEqual(caught.exception.reason, "consumed")

    def test_drift_also_burns_the_receipt(self):
        self.receipt()
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, reread=lambda dim: "expert")
        self.assertEqual(caught.exception.reason, "tier_drift")
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"],
                        "漂移的回执必须一起作废，否则会一直重试同一张")

    def test_reread_failure_blocks_send(self):
        """CARD-06b 第 1 条：读失败 = 没读到 = 拒发（06a 的"不拦"已作废）。"""
        self.receipt()
        def boom(dim):
            raise RuntimeError("COM 抽风")
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, reread=boom)
        self.assertEqual(caught.exception.reason, "tier_unreadable")
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"],
                        "读不到也要把回执作废，避免反复拿同一张试")

    def test_reread_none_means_unreadable_and_rejected(self):
        self.receipt()
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, reread=lambda dim: None)
        self.assertEqual(caught.exception.reason, "tier_unreadable")

    def test_threads_race_only_one_wins(self):
        self.receipt()
        winners, losers = [], []
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            ok = tg.consume("grok", root=self.tmp)["ok"]
            (winners if ok else losers).append(1)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(winners), 1, f"应只有 1 个消费成功，实际 {len(winners)}")
        self.assertEqual(len(losers), 7)

    def test_processes_race_only_one_wins(self):
        """真进程并发：文件锁必须拦得住，不是靠同进程的线程锁。

        子进程的等锁上限给到 30 秒：4 个解释器同时冷启动 + 磁盘 fsync，
        5 秒在这台机器上偶发不够（实测有一次真超时），但"只有一个赢"这件事不变。
        """
        self.receipt()
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from qi import tier_guard as tg\n"
            "print('OK' if tg.consume('grok', root=Path(%r), timeout=30)['ok'] else 'NO')\n"
        ) % (str(SRC), str(self.tmp))
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True) for _ in range(4)]
        results = []
        try:
            for proc in procs:
                out, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err)
                results.append(out.strip())
        finally:
            for proc in procs:                   # 断言失败时别把子进程留在临时目录里
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=30)
        self.assertEqual(results.count("OK"), 1, results)

    def test_lock_really_excludes_another_process(self):
        """持锁的子进程没放手之前，本进程必须等超时——证明用的是 OS 锁。"""
        path = tg.lock_path("grok", root=self.tmp)
        holder = (
            "import sys, time; sys.path.insert(0, %r)\n"
            "from qi import tier_guard as tg\n"
            "from pathlib import Path\n"
            "path = Path(%r)\n"
            "with tg.file_lock(path):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(3)\n"
        ) % (str(SRC), str(path))
        proc = subprocess.Popen([sys.executable, "-c", holder],
                                stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "HELD")
            with self.assertRaises(TimeoutError):
                with tg.file_lock(path, timeout=0.3):
                    pass
        finally:
            proc.kill()
            proc.communicate(timeout=30)


# ---------------------------------------------------------------- 4. 档位表
class TierTableTests(Base):
    def test_table_is_json_and_covers_every_endpoint_with_a_receipt(self):
        data = json.loads(TIERS.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        endpoints = data["endpoints"]
        # accept_lite 是 CARD-06c 第 6 条加的**独立**用途，只给 opencode 的 GLM 档，
        # 不要求每个端都登记（硬塞进 accept 才是不允许的）。其余用途必须齐全。
        required = [p for p in tg.PURPOSES if p != "accept_lite"]
        for name in ("chatgpt", "codex", "deepseek", "dsh", "gemini", "grok",
                     "opencode", "workbuddy"):
            self.assertIn(name, endpoints, f"档位表缺 {name}")
            for dimension, entry in endpoints[name]["dimensions"].items():
                for purpose in required:
                    self.assertIn(purpose, entry,
                                  f"{name}/{dimension} 缺 {purpose}")

    def test_accept_lite_is_opencode_only(self):
        """accept_lite 不许扩散：它一旦出现在别的端，就等于把低档验收混进正表。"""
        data = json.loads(TIERS.read_text(encoding="utf-8"))
        holders = []
        for name, endpoint in data["endpoints"].items():
            for dimension, entry in endpoint["dimensions"].items():
                if "accept_lite" in entry:
                    holders.append((name, dimension))
        self.assertEqual(holders, [("opencode", "model")])
        self.assertEqual(data["endpoints"]["opencode"]["dimensions"]["model"]["accept_lite"],
                         ["glm_53_flash_free"])

    def test_purposes_match_the_card(self):
        data = json.loads(TIERS.read_text(encoding="utf-8"))
        self.assertEqual(sorted(tg.PURPOSES), ["accept", "accept_lite", "review", "test"])
        self.assertIn("最低档", data["purposes"]["test"])
        self.assertIn("最高档", data["purposes"]["review"])
        self.assertIn("最高档", data["purposes"]["accept"])
        self.assertIn("PASS(GLM-lite)", data["purposes"]["accept_lite"])

    def test_lowest_and_highest_are_what_the_ui_actually_offers(self):
        self.assertEqual(tg.allowed_tiers("grok", "test", "model"), ["fast"])
        self.assertEqual(tg.allowed_tiers("grok", "accept", "model"), ["expert"])
        self.assertEqual(tg.allowed_tiers("gemini", "test", "model"), ["flash_lite"])
        self.assertEqual(tg.allowed_tiers("gemini", "accept", "model"), ["argon"])
        self.assertEqual(tg.allowed_tiers("opencode", "test", "effort"), ["default"])
        self.assertEqual(tg.allowed_tiers("opencode", "accept", "effort"), ["max"])
        self.assertEqual(tg.allowed_tiers("deepseek", "test", "think"), ["off"])

    def test_codex_ultra_is_the_top_effort(self):
        ladder = json.loads(TIERS.read_text(encoding="utf-8"))["endpoints"]["codex"][
            "dimensions"]["effort"]["ladder"]
        self.assertEqual(ladder[0], "low")
        self.assertEqual(ladder[-1], "ultra")

    def test_workbuddy_allowlist_matches_owner_authorization(self):
        """T4-1（2026-11-07 owner 授权）：test/accept 补成与 review 相同的模型。

        这里把授权值钉死，防止以后被随手改掉；同时钉死 `ladder` 必须留空
        —— WorkBuddy 没有权威的高低档排序，**不许从白名单反推档位高低**
        （那正是本卡当初"不许把不知道写成都一样"要防的事）。
        """
        self.assertEqual(tg.allowed_tiers("workbuddy", "test", "model"),
                         ["DeepSeek-V4.1-Flash"])
        self.assertEqual(tg.allowed_tiers("workbuddy", "accept", "model"),
                         ["DeepSeek-V4.1-Flash", "Hy4"])
        ladder = tg.load_tiers()["endpoints"]["workbuddy"]["dimensions"]["model"]["ladder"]
        self.assertEqual(ladder, [], "ladder 必须留空：没有权威高低排序，不许反推")

    def test_empty_allowlist_still_fails_closed(self):
        """门禁语义一个字没变：白名单为空时仍然必须拒发。

        用**显式 fixture** 把 accept 清空，不再依赖"生产配置恰好是空的"这个事实
        —— 生产配置现在有值了，门禁本身不能被它绑架。
        """
        tiers = tg.load_tiers()
        tiers["endpoints"]["workbuddy"]["dimensions"]["model"]["accept"] = []
        self.assertEqual(
            tg.allowed_tiers("workbuddy", "accept", "model", tiers=tiers), [])
        self.receipt(endpoint="workbuddy", target="DeepSeek-V4.1-Flash")
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("workbuddy", "accept", root=self.tmp, do_consume=False,
                    require_readback=False, tiers=tiers)
        self.assertEqual(caught.exception.reason, "table_unconfigured")

    def test_missing_dimension_is_reported(self):
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.allowed_tiers("grok", "test", "effort")
        self.assertEqual(caught.exception.reason, "dimension_not_in_table")


# ---------------------------------------------------------------- 5. 退出码
class ExitCodeTests(unittest.TestCase):
    def test_new_codes_do_not_collide(self):
        self.assertEqual(ec.TIER_UNCONFIRMED, 5)
        self.assertEqual(ec.UIA_DOCS_LOST, 6)
        self.assertEqual(ec.WINDOW_MINIMIZED, 7)
        codes = [ec.OK, ec.FAIL, ec.USAGE, ec.MISSING_ASSETS, ec.BUSY,
                 ec.TIER_UNCONFIRMED, ec.UIA_DOCS_LOST, ec.WINDOW_MINIMIZED]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(codes, list(range(8)))

    def test_stage_mapping(self):
        self.assertEqual(ec.code_for("tier_unconfirmed"), 5)
        self.assertEqual(ec.code_for("uia_docs_lost"), 6)
        self.assertEqual(ec.code_for("window_minimized"), 7)
        self.assertEqual(ec.code_for("user_busy"), 4)
        self.assertEqual(ec.code_for("bad_arg"), 2)
        self.assertEqual(ec.code_for("done"), 0)
        self.assertEqual(ec.code_for("something_new"), 1, "认不出来的 stage 必须按 1 算")
        self.assertEqual(ec.code_for(None), 1)

    def test_argparse_two_is_still_usage(self):
        self.assertEqual(ec.code_for("bad_args"), ec.USAGE)

    def test_doc_lists_every_code(self):
        text = DOC.read_text(encoding="utf-8")
        for code, label in ec.table():
            self.assertIn(f"| {code} |", text, f"docs/exit-codes.md 缺 {code} 这一行")
        self.assertIn("| **5** |", text)
        self.assertIn("| **6** |", text)
        self.assertIn("| **7** |", text)

    def test_doc_records_the_workflow_exception(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("workflow_use", text)
        self.assertIn("需要监工", text)

    def test_no_source_file_returns_five_or_six_by_accident(self):
        """5/6 只允许出现在新代码里。逐个文件扫裸 return 5/6，防止又各写各的。"""
        offenders = []
        for path in (SRC / "qi").glob("*.py"):
            if path.name in ("exit_codes.py", "tier_guard.py", "ui.py"):
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip() in ("return 5", "return 6"):
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------- 6. UIA 唤醒
class Clock:
    def __init__(self, step: float = 0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class WakeTests(unittest.TestCase):
    def setUp(self):
        ui.LAST_ROUNDS.clear()

    def test_warm_read_hits_and_never_wakes(self):
        doc = (object(), "https://grok.com/")
        with mock.patch.object(ui, "_recover") as rec:
            found = ui.candidates(7, read=lambda hwnd: [doc], sleep=lambda s: None,
                                  clock=Clock())
        self.assertEqual(found, [doc])
        rec.assert_not_called()
        self.assertEqual(ui.LAST_ROUNDS[0]["kind"], "warm")

    def test_empty_url_is_retried_without_waking(self):
        first, second = (object(), ""), (object(), "file:///x")
        reads = [[first], [second]]
        with mock.patch.object(ui, "_recover") as rec:
            found = ui.candidates(7, read=lambda hwnd: reads.pop(0),
                                  sleep=lambda s: None, clock=Clock())
        self.assertEqual(found, [second])
        rec.assert_not_called()

    def test_documents_lost_then_recovered_counts_rounds(self):
        doc = (object(), "oc://renderer/index.html")
        reads = [[], [], [doc]]
        sleeps = []
        with mock.patch.object(ui, "_recover", return_value={"ok": True}) as rec:
            found = ui.candidates(7, read=lambda hwnd: reads.pop(0),
                                  sleep=sleeps.append, clock=Clock())
        self.assertEqual(found, [doc])
        self.assertEqual(rec.call_count, 2, "第二轮回来的，就该只唤醒两次")
        self.assertEqual(sleeps, [ui.ROUND_INTERVAL])
        kinds = [r["kind"] for r in ui.LAST_ROUNDS]
        self.assertEqual(kinds, ["warm", "wake", "wake"])

    def test_gives_up_after_three_rounds(self):
        with mock.patch.object(ui, "_recover", return_value={"ok": True}) as rec:
            found = ui.candidates(7, read=lambda hwnd: [], sleep=lambda s: None,
                                  clock=Clock())
        self.assertEqual(found, [])
        self.assertEqual(rec.call_count, ui.MAX_ROUNDS)
        self.assertEqual(ui.MAX_ROUNDS, 3)

    def test_total_limit_stops_further_rounds(self):
        clock = Clock()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock.now += seconds

        with mock.patch.object(ui, "_recover", return_value={"ok": True}) as rec:
            ui.candidates(7, attempts=10, read=lambda hwnd: [], sleep=sleep, clock=clock)
        self.assertLessEqual(rec.call_count, 4,
                             "总限时 8 秒、每轮间隔 2 秒，最多 3~4 轮")
        self.assertTrue(any(r.get("kind") == "skipped" for r in ui.LAST_ROUNDS)
                        or rec.call_count < 10)

    def test_zero_documents_raises_exit_code_six(self):
        with mock.patch.object(ui, "candidates", return_value=[]), \
             mock.patch.object(ui, "window_facts", return_value="hwnd=1 前台=否"):
            with self.assertRaises(ui.UiDocsLost) as caught:
                ui.document(1, "workbuddy")
        exc = caught.exception
        self.assertEqual(exc.code, 6)
        self.assertEqual(exc.stage, "uia_docs_lost")
        self.assertIn("目标文档数量为 0，无法绑定", str(exc))
        self.assertIn("唤醒重试", str(exc))
        self.assertEqual(exc.as_result()["stage"], "uia_docs_lost")

    def test_uia_docs_lost_is_still_a_runtime_error(self):
        """老调用方都 `except RuntimeError`，换异常类不能把它们打穿。"""
        self.assertTrue(issubclass(ui.UiDocsLost, RuntimeError))

    def test_rounds_are_logged_to_runlog(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(ui, "_recover", return_value={"ok": True}), \
             mock.patch("qi.runlog.record_probe") as probe:
            ui.candidates(7, read=lambda hwnd: [], sleep=lambda s: None,
                          clock=Clock(), site="workbuddy")
        rounds = [c.kwargs.get("round") for c in probe.call_args_list]
        self.assertEqual(rounds, [0, 1, 2, 3], "每轮都要留一行（含最先那次直读）")
        self.assertEqual(probe.call_args_list[0].args[0], "workbuddy")

    def test_no_site_means_no_log_noise(self):
        with mock.patch.object(ui, "_recover", return_value={"ok": True}), \
             mock.patch("qi.runlog.record_probe") as probe:
            ui.candidates(7, read=lambda hwnd: [], sleep=lambda s: None,
                          clock=Clock())
        probe.assert_not_called()


# ---------------------------------------------------------------- 7. 最小化窗口
class FakeApi:
    """假 Win32：按帧序列回答 IsIconic / GetWindowRect。"""

    def __init__(self, frames, *, exists=True, show_return=False):
        self.frames = list(frames)
        self.exists = exists
        self.show_return = show_return
        self.shown = []

    def frame(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def is_window(self, hwnd):
        return self.exists

    def is_iconic(self, hwnd):
        return self.frame()[0]

    def show(self, hwnd, cmd):
        self.shown.append(cmd)
        return self.show_return

    def rect(self, hwnd):
        return self.frame()[1]


ICONIC = (-32000, -32000, 160, 28)
SANE = (-1440, -560, 1380, 1340)


class WindowTests(unittest.TestCase):
    def test_already_visible_needs_no_show(self):
        api = FakeApi([(False, SANE)])
        facts = window_mod.restore(5, api=api)
        self.assertTrue(facts["ok"])
        self.assertEqual(facts["reason"], "already")
        self.assertFalse(facts["show_called"])
        self.assertEqual(api.shown, [])

    def test_restores_and_polls_until_really_restored(self):
        api = FakeApi([(True, ICONIC), (True, ICONIC), (False, SANE)], show_return=False)
        facts = window_mod.restore(5, api=api, timeout=5.0, interval=0)
        self.assertTrue(facts["ok"])
        self.assertEqual(facts["reason"], "restored")
        self.assertFalse(facts["show_return"], "ShowWindow 返回 False 也要继续轮询")
        self.assertEqual(api.shown, [window_mod.SW_RESTORE])

    def test_failure_is_reported_not_raised(self):
        api = FakeApi([(True, ICONIC)])
        facts = window_mod.restore(5, api=api, timeout=0.0, interval=0)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["reason"], "iconic")
        self.assertEqual(facts["rect"], list(ICONIC))

    def test_ensure_raises_with_exit_code_seven(self):
        api = FakeApi([(True, ICONIC)])
        with self.assertRaises(window_mod.WindowMinimized) as caught:
            window_mod.ensure(5, api=api, timeout=0.0, interval=0)
        self.assertEqual(caught.exception.code, 7)
        self.assertEqual(caught.exception.stage, "window_minimized")
        self.assertEqual(caught.exception.facts["reason"], "iconic")

    def test_sentinel_without_iconic_is_still_rejected(self):
        api = FakeApi([(False, ICONIC)])
        facts = window_mod.restore(5, api=api, timeout=0.0, interval=0)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["reason"], "sentinel")

    def test_too_small_window_is_not_usable(self):
        api = FakeApi([(False, (10, 10, 100, 60))])
        facts = window_mod.restore(5, api=api, timeout=0.0, interval=0)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["reason"], "too_small")

    def test_dead_window(self):
        api = FakeApi([(False, None)], exists=False)
        facts = window_mod.restore(5, api=api)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["reason"], "gone")

    def test_usable_accepts_negative_coordinates(self):
        """副屏在主屏左边，合法窗口的 x 就是负的——不能因为负数就判成哨兵。"""
        self.assertTrue(window_mod.usable((-1440, -560, 1380, 1340)))
        self.assertFalse(window_mod.usable(ICONIC))

    def test_restored_rect_falls_back_to_none(self):
        api = FakeApi([(True, ICONIC)])
        self.assertIsNone(window_mod.restored_rect(5, api=api, timeout=0.0))

    def test_restored_rect_returns_after_restore(self):
        api = FakeApi([(True, ICONIC), (False, SANE)])
        self.assertEqual(window_mod.restored_rect(5, api=api, timeout=1.0), SANE)

    def test_screen_refuses_to_use_sentinel_coordinates(self):
        from qi.screen import Screen
        screen = Screen(verbose=False)
        screen.target_hwnd = 4242
        with mock.patch.object(window_mod, "restore",
                              return_value={"ok": False, "reason": "iconic",
                                            "rect": list(ICONIC), "waited": 5.0}):
            with self.assertRaises(window_mod.WindowMinimized) as caught:
                screen.bound_rect()
        self.assertEqual(caught.exception.code, 7)

    def test_screen_returns_the_real_rect_after_restore(self):
        from qi.screen import Screen
        screen = Screen(verbose=False)
        screen.target_hwnd = 4242
        with mock.patch.object(window_mod, "restore",
                              return_value={"ok": True, "reason": "restored",
                                            "rect": list(SANE), "waited": 0.3}):
            self.assertEqual(screen.bound_rect(), SANE)


# ---------------------------------------------------------------- 8. choose / use 接线
class ChooseWiringTests(unittest.TestCase):
    """choose 读回确认后确实写回执；最小化时退出码 7 而不是抛栈。"""

    def fixture(self, argv, *, choose_result=None, read_current=None):
        recorded = []
        namespace = {
            "argparse": argparse, "json": json,
            "time": types.SimpleNamespace(sleep=lambda s: None),
            "guard": types.SimpleNamespace(
                parse_guard_args=lambda values: (2, 120, True, values),
                guard=lambda *args: True),
            "Screen": _FakeScreen, "ensure_site": lambda name, tab=None: 123,
            "choose": lambda s, cfg, key: choose_result or {"ok": True, "after": key,
                                                           "stage": "done"},
            "read_current": read_current or (lambda s, cfg: {"state": "argon"}),
            "read_writer": lambda s, cfg: {"writer": "dialog"},
            "tier_guard": types.SimpleNamespace(
                record_choice=lambda *a, **kw: recorded.append((a, kw))),
            "WindowMinimized": _FakeMinimized,
            "ForegroundLost": _FakeForegroundLost,
            "zoom_mod": types.SimpleNamespace(ensure=lambda *a, **k: None,
                                              ZoomNot100=_FakeMinimized),
            "failure": types.SimpleNamespace(record=lambda *a, **k: None),
            "_fail": lambda endpoint, screen, hwnd, result, code: code,
            "_evidence": lambda *a, **k: None,
        }
        cfg = types.SimpleNamespace(name="gemini", anchor="x", open_offset=(0, 0),
                                    prefix="gem_", options={"argon": "Argon"},
                                    has_chip=("argon",), writers={}, title_hint="Chrome",
                                    on_secondary=True, writer_tabs=(), open_by_chip=True)
        cli = load_function(ROOT / "src" / "qi" / "choose.py", "cli", namespace)
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli(cfg, argv)
        return code, recorded

    def test_receipt_written_after_readback(self):
        code, recorded = self.fixture(["--model", "argon"])
        self.assertEqual(code, 0)
        self.assertEqual(len(recorded), 1)
        args, kwargs = recorded[0]
        self.assertEqual(args[0], "gemini")
        self.assertEqual(kwargs["target"], "argon")
        self.assertEqual(kwargs["readback"], "argon")
        self.assertEqual(kwargs["dimension"], "model")

    def test_menu_closed_verification_records_empty_readback(self):
        code, recorded = self.fixture(
            ["--model", "argon"],
            choose_result={"ok": True, "after": "argon", "verified_by": "menu_closed"})
        self.assertEqual(code, 0)
        self.assertIsNone(recorded[0][1]["readback"],
                          "只靠菜单关闭验证的档位没有读回值，必须记空")

    def test_failed_choice_writes_nothing(self):
        code, recorded = self.fixture(["--model", "argon"],
                                      choose_result={"ok": False, "stage": "unverified"})
        self.assertEqual(code, 1)
        self.assertEqual(recorded, [])

    def test_list_writes_nothing(self):
        code, recorded = self.fixture(["--list"])
        self.assertEqual(code, 0)
        self.assertEqual(recorded, [])

    def test_minimized_window_maps_to_exit_seven(self):
        def boom(s, cfg):
            raise _FakeMinimized("窗口最小化且没能恢复")
        code, recorded = self.fixture(["--list"], read_current=boom)
        self.assertEqual(code, 7)


class _FakeScreen:
    def __init__(self):
        self.target_hwnd = None

    def bind(self, hwnd):
        self.target_hwnd = hwnd
        return True

    def focus(self):
        return True


class _FakeForegroundLost(RuntimeError):
    """和 qi.screen.ForegroundLost 一样的独立类型（不是 WindowMinimized 的父类）。"""


class _FakeMinimized(RuntimeError):
    code = 7
    stage = "window_minimized"
    facts = {"reason": "iconic", "rect": list(ICONIC)}

    def as_result(self):
        return {"ok": False, "stage": self.stage, "code": self.code, "hint": str(self)}


class UseWiringTests(unittest.TestCase):
    """use 的发送闸：没有回执 → 5；--purpose 缺失 → 2；stage 映射不吞成 1。"""

    def test_purpose_is_required_for_sending(self):
        import qi.flow as flow
        site = types.SimpleNamespace(name="chatgpt")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                flow.cli(site, ["你好"])
        self.assertEqual(caught.exception.code, 2)

    def test_gemini_use_requires_purpose_but_read_does_not(self):
        import qi.gemini as gemini
        site = types.SimpleNamespace(name="gemini")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                gemini.cli(site, ["你好"])
        self.assertEqual(caught.exception.code, 2)

    def test_tier_unconfirmed_maps_to_five(self):
        import qi.flow as flow
        site = types.SimpleNamespace(name="chatgpt")
        failure = tg.TierUnconfirmed("receipt_missing", "先跑 chatgpt_choose").as_result()
        with mock.patch("qi.flow.guard.guard", return_value=True), \
             mock.patch.object(flow, "ask", return_value=failure), \
             mock.patch("qi.runlog.record"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = flow.cli(site, ["你好", "--purpose", "test"])
        self.assertEqual(code, 5)

    def test_window_minimized_maps_to_seven(self):
        import qi.flow as flow
        site = types.SimpleNamespace(name="chatgpt")
        with mock.patch("qi.flow.guard.guard", return_value=True), \
             mock.patch.object(flow, "ask",
                               side_effect=window_mod.WindowMinimized("卡住了", {})), \
             mock.patch("qi.runlog.record"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = flow.cli(site, ["你好", "--purpose", "test"])
        self.assertEqual(code, 7)

    def test_uia_docs_lost_maps_to_six(self):
        import qi.flow as flow
        site = types.SimpleNamespace(name="chatgpt")
        with mock.patch("qi.flow.guard.guard", return_value=True), \
             mock.patch.object(flow, "ask", side_effect=ui.UiDocsLost("读不到", [], "")), \
             mock.patch("qi.runlog.record"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = flow.cli(site, ["你好", "--purpose", "test"])
        self.assertEqual(code, 6)

    def test_gate_or_exit_prints_json_and_returns_five(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            code = tg.gate_or_exit("grok", "test", out=lambda text: out.write(text + "\n"),
                                   root=Path(folder))
        self.assertEqual(code, 5)
        payload = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["stage"], "tier_unconfirmed")
        self.assertEqual(payload["code"], 5)

    def test_workbuddy_list_needs_no_purpose(self):
        from qi import workbuddy
        _, args = workbuddy.parse_args(["--list"], sending=True)
        self.assertIsNone(args.purpose)


if __name__ == "__main__":
    unittest.main()
