"""CARD-09a：本地后端 server 的离线测试。

全部离线：子进程是假的（`FakeRunner`），不开窗口、不点 UI、不发消息、不碰真实
`progress.json`。真机只做 /health、/endpoints 两次请求，见 evidence\\card09a_live.txt。

覆盖卡面第 9 条列的每一项：

    1 token / state      首启生成、重启沿用、rotate、坏文件、打码
    2 HTTP 边界          401 / 错 Host / 错 Origin / token 进 URL / OPTIONS 预检
    3 /endpoints         只读 docs\\tiers.json
    4 /ask + /jobs       202 + 轮询、失败隔离、choose 不过不跑 use、去重、429、
                         重启后从磁盘读 job、空档位表
    5 执行约束           超时杀进程树、输出截断、use 失败分端记
    6 UI 文件锁          跨进程互斥、持锁进程死掉自动释放、可重入
    7 /workflow          白名单、非法迁移 409、并发只成功一次、写入后仍是合法 JSON
    8 端口占用           第二次绑定必须失败
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = SRC / "scripts"
for entry in (str(SRC), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import server, tier_guard, uilock  # noqa: E402

import bridge_server as bridge_cli  # noqa: E402

TOKEN = "test-token-" + "a" * 24
ENDPOINTS = ("grok", "gemini", "deepseek")


# ================================================================ 假子进程
class FakeRunner:
    """按脚本名返回结果；choose 会顺手写一张真的档位回执。"""

    def __init__(self, root: Path, *, fail_choose=(), fail_use=(), mismatch=(),
                 timeout_use=(), block: threading.Event | None = None):
        self.root = Path(root)
        self.fail_choose = set(fail_choose)
        self.fail_use = set(fail_use)
        self.mismatch = set(mismatch)
        self.timeout_use = set(timeout_use)
        self.block = block
        self.calls: "list[list[str]]" = []
        self.started = threading.Event()

    def __call__(self, cmd, *, timeout, cwd, env=None):
        self.calls.append(list(cmd))
        script = Path(cmd[1]).name
        endpoint = script.split("_")[0]
        if script.endswith("_choose.py"):
            if self.block is not None:
                self.started.set()
                self.block.wait(5)
            if endpoint in self.fail_choose:
                return {"ok": False, "timeout": False, "exit_code": 1, "stdout": "",
                        "stderr": "choose boom", "elapsed": 0.01}
            flag = next((x for x in cmd if x.startswith("--")), "--model")
            target = cmd[cmd.index(flag) + 1]
            readback = "wrong-" + target if endpoint in self.mismatch else target
            tier_guard.write_receipt(endpoint, target=target, readback=readback,
                                     root=self.root)
            return {"ok": True, "timeout": False, "exit_code": 0,
                    "stdout": '{"ok": true, "stage": "done"}', "stderr": "",
                    "elapsed": 0.01}
        if script.endswith("_use.py"):
            if endpoint in self.timeout_use:
                return {"ok": False, "timeout": True, "exit_code": None,
                        "stdout": "", "stderr": "", "elapsed": 5.0}
            if endpoint in self.fail_use:
                return {"ok": False, "timeout": False, "exit_code": 1,
                        "stdout": json.dumps({"ok": False, "stage": "no_reply",
                                              "hint": "no reply"}), "stderr": "",
                        "elapsed": 0.1}
            return {"ok": True, "timeout": False, "exit_code": 0,
                    "stdout": json.dumps({"ok": True, "stage": "answered",
                                          "reply": f"reply:{endpoint}"}),
                    "stderr": "", "elapsed": 0.1}
        raise AssertionError(f"unexpected command: {cmd}")

    def scripts(self) -> "list[str]":
        return [Path(cmd[1]).name for cmd in self.calls]


def make_root(tmp: Path) -> Path:
    """把 docs 和脚本壳子铺到临时根目录，Engine 只认这个 root。"""
    (tmp / "docs" / "workflow").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "tiers.json", tmp / "docs" / "tiers.json")
    shutil.copy2(ROOT / "docs" / "workflow" / "workflow.json",
                 tmp / "docs" / "workflow" / "workflow.json")
    (tmp / "src" / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("grok", "gemini", "deepseek", "workbuddy"):
        for suffix in ("choose", "use"):
            (tmp / "src" / "scripts" / f"{name}_{suffix}.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")
    return tmp


def write_progress(root: Path, cards: dict, name: str = "progress.json") -> Path:
    path = root / name
    path.write_text(json.dumps({
        "card_id": "T8", "status": "running", "note": "", "updated_at": "2026-11-02T12:00:00+08:00",
        "workflow": {"cards": cards},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class ServerCase(unittest.TestCase):
    config = {"queue_limit": 4, "choose_timeout": 5.0, "use_timeout": 5.0,
              "endpoint_timeout": 15.0, "ui_lock_timeout": 5.0}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))
        self.runner = self.make_runner()
        self.engine = server.Engine(root=self.root, token=TOKEN, port=0,
                                    config=self.config, runner=self.runner)
        self.httpd = server.create_server(0, self.engine)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def make_runner(self):
        return FakeRunner(self.root)

    def tearDown(self):
        self.engine.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    # ------------------------------------------------------------ HTTP 助手
    def request(self, method: str, path: str, body=None, *, token: str | None = TOKEN,
                origin=None, host=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        headers.update(extra_headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        if host is None:
            conn.request(method, path, body=data, headers=headers)
        else:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(data or b"")))
            conn.endheaders(data)
        response = conn.getresponse()
        raw = response.read()
        result = dict(response.getheaders())
        conn.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        return response.status, payload, result

    def ask(self, endpoints, *, purpose="test", prompt="hi", request_id=None):
        body = {"endpoints": list(endpoints), "prompt": prompt, "purpose": purpose}
        if request_id:
            body["request_id"] = request_id
        return self.request("POST", "/ask", body)


# ================================================================ 1. token / state
class TokenStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"BRIDGE_SERVER_STATE": str(self.dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def test_first_start_creates_and_restart_reuses_token(self):
        first = server.load_or_create_state(port=18765)
        self.assertTrue(first["created"])
        self.assertGreaterEqual(len(first["token"]), 32)
        on_disk = json.loads(server.state_path().read_text(encoding="utf-8"))
        self.assertEqual(on_disk["token"], first["token"])
        self.assertEqual(on_disk["port"], 18765)

        second = server.load_or_create_state(port=18766)
        self.assertFalse(second["created"])
        self.assertEqual(second["token"], first["token"])
        self.assertEqual(second["port"], 18766)
        again = json.loads(server.state_path().read_text(encoding="utf-8"))
        self.assertEqual(again["token"], first["token"])
        self.assertEqual(again["port"], 18766)

    def test_rotate_changes_token(self):
        first = server.load_or_create_state(port=18765)
        rotated = server.rotate_token()
        self.assertNotEqual(rotated["token"], first["token"])
        self.assertEqual(server.load_state()["token"], rotated["token"])

    def test_corrupt_state_is_replaced(self):
        server.state_path().parent.mkdir(parents=True, exist_ok=True)
        server.state_path().write_text("not json", encoding="utf-8")
        state = server.load_or_create_state(port=18765)
        self.assertTrue(state["created"])
        self.assertTrue(server.load_state())

    def test_mask_token_keeps_middle_secret(self):
        masked = server.mask_token("abcdefgh12345678")
        self.assertEqual(masked, "abcd...78")
        self.assertNotIn("efgh1234", masked)


# ================================================================ 2. UI 文件锁
class UiLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_metadata_records_pid_owner_and_time(self):
        with uilock.hold("unit", timeout=2, root=self.root):
            meta = uilock.describe(self.root)
            self.assertTrue(meta["locked"])
            self.assertEqual(meta["pid"], os.getpid())
            self.assertEqual(meta["owner"], "unit")
            self.assertIn("acquired_at", meta)
        after = uilock.describe(self.root)
        self.assertFalse(after["locked"])
        self.assertIn("released_at", after)

    def test_second_thread_times_out(self):
        started = threading.Event()

        def holder():
            with uilock.hold("holder", timeout=5, root=self.root):
                started.set()
                time.sleep(0.4)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(started.wait(3))
        try:
            with self.assertRaises(uilock.Busy) as ctx:
                with uilock.hold("waiter", timeout=0.1, root=self.root):
                    pass
            self.assertIn("holder", str(ctx.exception))
            self.assertEqual(ctx.exception.code, 8)
        finally:
            thread.join(timeout=3)
        with uilock.hold("after", timeout=2, root=self.root):
            pass

    def test_reentrant_in_same_thread(self):
        with uilock.hold("outer", timeout=2, root=self.root):
            with uilock.hold("inner", timeout=0.2, root=self.root):
                pass

    def test_dead_metadata_does_not_block(self):
        path = uilock.lock_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = uilock._open(path)
        try:
            uilock._write_meta(handle, {"pid": 999999, "owner": "ghost",
                                        "acquired_at": "2000-01-01T00:00:00+0800"})
        finally:
            handle.close()
        with uilock.hold("live", timeout=2, root=self.root):
            self.assertEqual(uilock.describe(self.root)["owner"], "live")

    def test_holder_process_death_releases_lock(self):
        code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from qi import uilock\n"
            "ctx = uilock.hold('child', timeout=5, root=%r)\n"
            "ctx.__enter__()\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n" % str(self.root)
        )
        child = subprocess.Popen([sys.executable, "-c", code],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            start = time.monotonic()
            with self.assertRaises(uilock.Busy):
                with uilock.hold("parent", timeout=0.2, root=self.root):
                    pass
            self.assertLess(time.monotonic() - start, 2)
        finally:
            child.kill()
            child.wait(timeout=5)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        with uilock.hold("parent", timeout=5, root=self.root):
            self.assertEqual(uilock.describe(self.root)["owner"], "parent")


# ================================================================ 3. HTTP 边界
class HttpGuardTests(ServerCase):
    def test_health_needs_no_token(self):
        status, body, _ = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(set(body), {"ok", "version"})
        self.assertNotIn("token", json.dumps(body))

    def test_endpoints_without_token_is_401(self):
        status, body, headers = self.request("GET", "/endpoints", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))

    def test_endpoints_with_wrong_token_is_401(self):
        status, body, _ = self.request("GET", "/endpoints", token="nope")
        self.assertEqual(status, 401)

    def test_wrong_host_is_403(self):
        status, body, _ = self.request("GET", "/endpoints", host="evil.example:18765")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_host")

    def test_non_extension_origin_is_403(self):
        status, body, _ = self.request("GET", "/endpoints", origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_origin")

    def test_token_in_url_is_rejected(self):
        status, body, _ = self.request("GET", "/endpoints?token=" + TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "token_in_url")

    def test_options_preflight_echoes_extension_origin(self):
        status, _, headers = self.request(
            "OPTIONS", "/ask", token=None, origin="chrome-extension://abcdefgh")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "chrome-extension://abcdefgh")
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("Content-Type", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))

    def test_options_without_origin_is_allowed(self):
        status, _, headers = self.request("OPTIONS", "/ask", token=None)
        self.assertEqual(status, 204)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_options_bad_origin_is_403(self):
        status, body, _ = self.request("OPTIONS", "/ask", token=None,
                                       origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_origin")

    def test_health_echoes_extension_origin(self):
        status, _, headers = self.request("GET", "/health", token=None,
                                          origin="chrome-extension://xyz")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "chrome-extension://xyz")


class EndpointsTests(ServerCase):
    def test_endpoints_reads_tiers_table(self):
        status, body, _ = self.request("GET", "/endpoints")
        self.assertEqual(status, 200)
        names = [row["name"] for row in body["endpoints"]]
        self.assertIn("grok", names)
        self.assertIn("workbuddy", names)
        self.assertEqual(body["purposes"], ["test", "review", "accept"])
        grok = next(row for row in body["endpoints"] if row["name"] == "grok")
        self.assertEqual(grok["dimensions"]["model"]["test"], ["fast"])
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_endpoints_hides_internal_tier_lists(self):
        _, body, _ = self.request("GET", "/endpoints")
        self.assertNotIn("accept_lite", json.dumps(body))


# ================================================================ 4. /ask + /jobs
class AskTests(ServerCase):
    config = {"queue_limit": 1, "choose_timeout": 5.0, "use_timeout": 5.0,
              "endpoint_timeout": 15.0, "ui_lock_timeout": 5.0}

    def test_ask_202_then_poll_done(self):
        status, body, _ = self.ask(["grok"], purpose="test", prompt="hello")
        self.assertEqual(status, 202)
        self.assertTrue(body["job_id"])
        self.assertFalse(body["duplicate"])

        job = self.engine.wait_job(body["job_id"], 5)
        self.assertEqual(job["status"], "done")
        record = job["endpoints"]["grok"]
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["reply"], "reply:grok")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["choose"]["target"], "fast")
        self.assertTrue(record["choose"]["confirmed"])

        status, body, _ = self.request("GET", f"/jobs/{body['job_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["status"], "done")

    def test_failure_is_isolated_per_endpoint(self):
        self.runner.fail_choose = {"gemini"}
        status, body, _ = self.ask(["grok", "gemini"], purpose="review")
        self.assertEqual(status, 202)
        job = self.engine.wait_job(body["job_id"], 5)
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["endpoints"]["grok"]["status"], "done")
        self.assertEqual(job["endpoints"]["gemini"]["status"], "failed")
        self.assertEqual(job["endpoints"]["gemini"]["error_code"], "choose_failed")
        self.assertNotIn("gemini_use.py", self.runner.scripts())

    def test_choose_not_confirmed_skips_use(self):
        self.runner.mismatch = {"grok"}
        _, body, _ = self.ask(["grok"])
        job = self.engine.wait_job(body["job_id"], 5)
        record = job["endpoints"]["grok"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "readback_mismatch")
        self.assertIsNone(record["use"])
        self.assertEqual([name for name in self.runner.scripts() if name.endswith("_use.py")], [])

    def test_use_failure_is_reported_separately(self):
        self.runner.fail_use = {"grok"}
        _, body, _ = self.ask(["grok"])
        job = self.engine.wait_job(body["job_id"], 5)
        record = job["endpoints"]["grok"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "no_reply")
        self.assertEqual(record["exit_code"], 1)
        self.assertEqual(record["error"]["exit_code"], 1)

    def test_use_timeout_recorded(self):
        self.runner.timeout_use = {"grok"}
        _, body, _ = self.ask(["grok"])
        job = self.engine.wait_job(body["job_id"], 5)
        record = job["endpoints"]["grok"]
        self.assertEqual(record["error_code"], "use_timeout")
        self.assertTrue(record["use"]["timeout"])

    def test_request_id_dedupes(self):
        status1, first, _ = self.ask(["grok"], request_id="req-001")
        status2, second, _ = self.ask(["grok"], request_id="req-001")
        self.assertEqual(status1, 202)
        self.assertEqual(status2, 202)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.engine.wait_job(first["job_id"], 5)
        self.assertEqual(len([n for n in self.runner.scripts() if n == "grok_choose.py"]), 1)

    def test_queue_limit_returns_429(self):
        block = threading.Event()
        self.runner.block = block
        try:
            status, first, _ = self.ask(["grok"])
            self.assertEqual(status, 202)
            self.assertTrue(self.runner.started.wait(3))
            status, body, _ = self.ask(["gemini"])
            self.assertEqual(status, 429)
            self.assertEqual(body["error"], "queue_full")
        finally:
            block.set()
        self.engine.wait_job(first["job_id"], 5)

    def test_restart_reads_job_from_disk(self):
        _, body, _ = self.ask(["grok"])
        job_id = body["job_id"]
        self.engine.wait_job(job_id, 5)
        path = self.root / "runs" / "jobs" / f"{job_id}.json"
        self.assertTrue(path.exists())

        restarted = server.Engine(root=self.root, token="other-token", port=0,
                                  config=self.config, runner=self.runner)
        job = restarted.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["endpoints"]["grok"]["reply"], "reply:grok")
        self.assertIn(job_id, [row["job_id"] for row in restarted.list_jobs()])

    def test_empty_tier_table_fails_closed(self):
        # 显式把这份临时副本的 workbuddy 白名单清空：门禁语义（空表必拒发）必须
        # 继续被覆盖，不能依赖"生产配置恰好是空的"——T4-1 之后它已经有值了。
        path = self.root / "docs" / "tiers.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("test", "review", "accept"):
            data["endpoints"]["workbuddy"]["dimensions"]["model"][key] = []
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        _, body, _ = self.ask(["workbuddy"])
        job = self.engine.wait_job(body["job_id"], 5)
        record = job["endpoints"]["workbuddy"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "tier_table_empty")
        self.assertNotIn("workbuddy_use.py", self.runner.scripts())

    def test_missing_purpose_is_400(self):
        status, body, _ = self.request("POST", "/ask",
                                       {"endpoints": ["grok"], "prompt": "hi"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_purpose")

    def test_accept_lite_is_not_a_server_purpose(self):
        status, body, _ = self.ask(["grok"], purpose="accept_lite")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_purpose")

    def test_unknown_endpoint_is_400(self):
        status, body, _ = self.ask(["nope"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "endpoint_not_allowed")

    def test_empty_prompt_is_400(self):
        status, body, _ = self.ask(["grok"], prompt="   ")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_prompt")

    def test_bad_request_id_is_400(self):
        status, body, _ = self.ask(["grok"], request_id="bad id/with space")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request_id")

    def test_job_not_found_is_404(self):
        status, body, _ = self.request("GET", "/jobs/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "job_not_found")

    def test_post_ask_without_token_is_401(self):
        status, body, _ = self.request(
            "POST", "/ask", {"endpoints": ["grok"], "prompt": "hi", "purpose": "test"},
            token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")


# ================================================================ 5. 进程与超时
class ProcessTests(unittest.TestCase):
    def test_timeout_kills_process_tree(self):
        class FakePopen:
            pid = 4321
            returncode = -9

            def __init__(self, *args, **kwargs):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("cmd", timeout)
                return b"", b""

            def kill(self):
                pass

        with mock.patch.object(server.subprocess, "Popen", FakePopen), \
                mock.patch.object(server, "terminate_tree") as killer:
            result = server.run_process(["python", "x.py"], timeout=1)
        self.assertTrue(result["timeout"])
        self.assertIsNone(result["exit_code"])
        killer.assert_called_once()

    def test_truncate_keeps_head_and_tail(self):
        text = "A" * 100 + "B" * 100
        out = server.truncate(text, 40)
        self.assertIn("truncated", out)
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("B"))

    def test_parse_last_json_ignores_noise(self):
        parsed = server.parse_last_json('lib 5/5\n{"ok": true, "stage": "done"}\n')
        self.assertEqual(parsed["stage"], "done")
        self.assertIsNone(server.parse_last_json("no json here"))


# ================================================================ 6. 端口占用
class PortTests(ServerCase):
    def test_second_bind_fails(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind((server.HOST, 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        engine = server.Engine(root=self.root, token=TOKEN, port=port,
                               config=self.config, runner=self.runner)
        try:
            with self.assertRaises(OSError):
                server.create_server(port, engine)
        finally:
            holder.close()


# ================================================================ 7. /workflow
class WorkflowTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.cards = {"T8": {"state": "draft", "no_progress": 0, "parent": None,
                             "missing": [], "history": []}}
        write_progress(self.root, self.cards)

    def move(self, to, *, card="T8", file="progress.json", **extra):
        body = {"file": file, "card": card, "to": to}
        body.update(extra)
        return self.request("POST", "/workflow", body)

    def test_get_returns_progress(self):
        status, body, _ = self.request("GET", "/workflow?file=progress.json")
        self.assertEqual(status, 200)
        self.assertTrue(body["exists"])
        self.assertEqual(body["data"]["workflow"]["cards"]["T8"]["state"], "draft")

    def test_get_missing_progress_test_is_empty(self):
        status, body, _ = self.request("GET", "/workflow?file=progress-test.json")
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])
        self.assertEqual(body["data"], {})

    def test_path_traversal_is_rejected(self):
        status, body, _ = self.request("GET", "/workflow?file=../progress.json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_file")
        status, body, _ = self.move("sent", file="../progress.json")
        self.assertEqual(status, 400)

    def test_illegal_transition_is_409(self):
        status, body, _ = self.move("done")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "illegal_transition")
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workflow"]["cards"]["T8"]["state"], "draft")

    def test_valid_chain_draft_sent_running_done(self):
        status, body, _ = self.move("sent")
        self.assertEqual(status, 200)
        self.assertEqual(body["from"], "draft")
        status, body, _ = self.move("running")
        self.assertEqual(status, 200)
        status, body, _ = self.move("done")
        self.assertEqual(status, 200)
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        card = data["workflow"]["cards"]["T8"]
        self.assertEqual(card["state"], "done")
        self.assertEqual([row["to"] for row in card["history"]], ["sent", "running", "done"])

    def test_create_test_card(self):
        status, body, _ = self.move("draft", card="T9", create=True)
        self.assertEqual(status, 200)
        self.assertTrue(body["created"])
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["workflow"]["cards"]["T9"]["state"], "draft")

    def test_missing_card_without_create_is_404(self):
        status, body, _ = self.move("sent", card="T9")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "card_not_found")

    def test_concurrent_same_transition_only_one_wins(self):
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(self.move("sent")[0])

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(results), [200, 409])
        raw = (self.root / "progress.json").read_text(encoding="utf-8")
        data = json.loads(raw)                      # 写完仍是合法 JSON
        card = data["workflow"]["cards"]["T8"]
        self.assertEqual(card["state"], "sent")
        self.assertEqual(len(card["history"]), 1)

    def test_missing_file_is_404_for_post(self):
        (self.root / "progress.json").unlink()
        status, body, _ = self.move("sent")
        self.assertEqual(status, 404)

    def test_get_workflow_without_token_is_401(self):
        status, body, _ = self.request("GET", "/workflow?file=progress.json", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")


# ================================================================ 8. 生命周期命令
def _quiet(func, *args, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = func(*args, **kwargs)
    return code, json.loads(buffer.getvalue())


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((server.HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))
        self.env = mock.patch.dict(
            os.environ, {"BRIDGE_SERVER_STATE": str(self.root / "state")})
        self.env.start()
        self.holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.holder.bind((server.HOST, 0))
        self.holder.listen(1)
        self.port = self.holder.getsockname()[1]

    def tearDown(self):
        self.holder.close()
        self.env.stop()
        self._tmp.cleanup()

    def test_start_refuses_occupied_port(self):
        code, out = _quiet(bridge_cli.start, self.port)
        self.assertEqual(code, 1)
        self.assertEqual(out["error"], "port_in_use")
        self.assertFalse(server.pid_path().exists())

    def test_start_refuses_when_pid_is_alive(self):
        port = _free_port()
        server.write_pid(port)                   # 当前进程，一定活着
        # The port deliberately has no listener. Model connection refusal
        # explicitly rather than probing an unowned local service.
        with mock.patch.object(bridge_cli.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = ConnectionRefusedError("offline fixture")
            code, out = _quiet(bridge_cli.start, port)
        self.assertEqual(code, 1)
        self.assertEqual(out["error"], "already_running")
        self.assertEqual(out["pid"], os.getpid())

    def test_status_reports_not_running(self):
        port = _free_port()
        with mock.patch.object(bridge_cli.server, "load_config",
                               return_value={"port": port}), \
             mock.patch.object(bridge_cli.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = ConnectionRefusedError("offline fixture")
            code, out = _quiet(bridge_cli.status)
        self.assertEqual(code, 1)
        self.assertFalse(out["running"])
        self.assertEqual(out["token"], "***")


if __name__ == "__main__":
    unittest.main()
