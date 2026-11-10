"""CARD-09b：插件移植 + 配对 的离线测试。

全部离线，不起浏览器、不装插件、不开窗口、不发 AI：

    1 清单静态        MV3 合法、固定 key 与扩展 ID、最小权限、CSP 精确串、
                      无 default_popup、无 content_scripts、没有残留 popup 文件
    2 扩展代码静态    无 storage.sync、无 console、无 innerHTML/eval、
                      token 走 storage.local + TRUSTED_CONTEXTS、3 秒 /health 超时、
                      24h job 清理、单例轮询（无 setInterval）
    3 后端 Origin    正确 ID 放行、错 ID 403、无 Origin 仍要 token、OPTIONS 预检、
                      /health 只回最小字段、compare_digest 真被调用、非 ASCII token 不炸 500
    4 token 文件      icacls 收紧到当前用户；失败只警告不回滚写入
    5 轮询单例        用 node 跑真模块：连点 start() 只有一个定时器（没有 node 就跳过）
    6 文档            README / .gitignore 与本卡一致
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import base64
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXT = SRC / "chrome-extension"
CI = ROOT / "docs" / "bridge_server.json"
for entry in (str(SRC), str(SRC / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import server  # noqa: E402

EXT_ID = "odjanlclocbikpjlkcfepnfbcejlapkd"
ORIGIN = "chrome-extension://" + EXT_ID
OTHER_ORIGIN = "chrome-extension://" + "a" * 32
TOKEN = "test-token-" + "b" * 24
JS_FILES = ("background.js", "bridge-api.js", "poll-scheduler.js", "sidepanel.js")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def js_source(name: str) -> str:
    return (EXT / name).read_text(encoding="utf-8")


def all_js() -> dict:
    return {name: js_source(name) for name in JS_FILES}


def extension_id_from_key(key_b64: str) -> str:
    """Chrome 的算法：SHA256(SPKI DER) 前 16 字节，每个 hex 位映射到 a-p。"""
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


# ================================================================ 1. 清单
class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = read_json(EXT / "manifest.json")

    def test_is_valid_manifest_v3(self):
        self.assertEqual(self.manifest["manifest_version"], 3)
        self.assertEqual(self.manifest["minimum_chrome_version"], "116")
        self.assertIsInstance(self.manifest["name"], str)
        self.assertIsInstance(self.manifest["description"], str)

    def test_version_is_009_and_no_shell_wording(self):
        self.assertEqual(self.manifest["version"], "0.0.9")
        blob = self.manifest["name"] + self.manifest["description"]
        self.assertNotIn("空壳", blob)
        self.assertIn("0.0.9", self.manifest["description"])

    def test_key_fixes_the_extension_id(self):
        key = self.manifest.get("key")
        self.assertIsInstance(key, str)
        self.assertEqual(extension_id_from_key(key), EXT_ID)
        self.assertEqual(len(base64.b64decode(key)), 294)      # RSA-2048 SPKI

    def test_allowed_extension_id_matches_manifest_key(self):
        config = read_json(CI)
        self.assertEqual(config["allowed_extension_id"], EXT_ID)
        self.assertEqual(server.normalize_extension_id(config["allowed_extension_id"]), EXT_ID)

    def test_private_key_is_not_in_the_repo(self):
        leaked = sorted(str(path) for path in ROOT.rglob("*.pem")
                        if ".git" not in path.parts and "evidence" not in path.parts)
        self.assertEqual(leaked, [])
        self.assertIn("*.pem", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_permissions_are_minimal(self):
        self.assertEqual(self.manifest["permissions"], ["storage", "sidePanel"])
        self.assertEqual(self.manifest["host_permissions"], ["http://127.0.0.1:18765/*"])
        blob = json.dumps(self.manifest)
        self.assertNotIn("<all_urls>", blob)
        self.assertNotIn("chrome://", blob)

    def test_csp_strings_are_exact(self):
        policy = self.manifest["content_security_policy"]["extension_pages"]
        self.assertIn("connect-src 'self' http://127.0.0.1:18765", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertNotIn("connect-src 'none'", policy)
        self.assertNotIn("unsafe", policy)

    def test_no_default_popup(self):
        self.assertNotIn("default_popup", self.manifest["action"])
        self.assertTrue(self.manifest["action"]["default_icon"]["128"].endswith(".png"))

    def test_side_panel_points_at_sidepanel(self):
        self.assertEqual(self.manifest["side_panel"]["default_path"], "sidepanel.html")
        self.assertTrue((EXT / "sidepanel.html").is_file())
        self.assertNotIn("view=", self.manifest["side_panel"]["default_path"])

    def test_no_content_scripts(self):
        self.assertNotIn("content_scripts", self.manifest)
        self.assertNotIn("web_accessible_resources", self.manifest)

    def test_old_popup_files_are_gone(self):
        for name in ("popup.html", "popup.js"):
            self.assertFalse((EXT / name).exists(), name)

    def test_background_worker_declared(self):
        self.assertEqual(self.manifest["background"]["service_worker"], "background.js")
        self.assertTrue((EXT / "background.js").is_file())

    def test_icons_exist(self):
        for size in ("16", "32", "48", "128"):
            self.assertTrue((EXT / self.manifest["icons"][size]).is_file(), size)


# ================================================================ 2. 后台与界面代码
class BackgroundTests(unittest.TestCase):
    def test_background_only_opens_the_side_panel(self):
        source = js_source("background.js")
        self.assertIn("setPanelBehavior", source)
        self.assertIn("openPanelOnActionClick: true", source)
        self.assertIn("chrome.sidePanel", source)
        # 只做这一件事：没有监听、没有定时器、不碰 tab、不读存储。
        for banned in ("addListener", "setInterval", "setTimeout", "chrome.tabs",
                       "chrome.storage", "chrome.scripting", "popup.html"):
            self.assertNotIn(banned, source)


class ExtensionCodeTests(unittest.TestCase):
    def test_no_sync_storage(self):
        for name, source in all_js().items():
            self.assertNotIn("storage.sync", source, name)

    def test_no_console_output_at_all(self):
        for name, source in all_js().items():
            self.assertNotIn("console.", source, name)

    def test_no_html_injection_or_eval(self):
        for name, source in all_js().items():
            self.assertNotIn("innerHTML", source, name)
            self.assertNotIn("outerHTML", source, name)
            self.assertNotIn("insertAdjacentHTML", source, name)
            self.assertNotIn("eval(", source, name)
            self.assertNotIn("new Function", source, name)

    def test_no_content_script_injection(self):
        for name, source in all_js().items():
            self.assertNotIn("chrome.scripting", source, name)
            self.assertNotIn("executeScript", source, name)

    def test_token_uses_local_storage_with_trusted_contexts(self):
        source = js_source("sidepanel.js")
        self.assertIn("chrome.storage.local", source)
        self.assertIn("setAccessLevel", source)
        self.assertIn('accessLevel: "TRUSTED_CONTEXTS"', source)
        self.assertNotIn("storage.session", source)

    def test_token_key_and_input_hygiene(self):
        html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
        self.assertIn('id="token-input"', html)
        self.assertIn('type="password"', html)
        self.assertIn('id="token-save"', html)
        self.assertIn('id="token-clear"', html)
        # 存完就把输入框清空，不让 token 停在 DOM 里。
        self.assertIn('$("token-input").value = ""', js_source("sidepanel.js"))

    def test_health_timeout_is_three_seconds(self):
        source = js_source("bridge-api.js")
        match = re.search(r"HEALTH_TIMEOUT_MS\s*=\s*(\d+)", source)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), 3000)
        self.assertLessEqual(int(match.group(1)), 10000)      # 卡面 F5④：≤10s

    def test_api_base_is_the_loopback_port(self):
        source = js_source("bridge-api.js")
        self.assertIn('export const BASE = "http://127.0.0.1:18765"', source)
        self.assertNotIn("localhost", source)

    def test_purpose_is_required_on_ask(self):
        source = js_source("bridge-api.js")
        self.assertIn("purpose", source)
        self.assertIn("request_id", source)
        panel = js_source("sidepanel.js")
        self.assertIn("purpose: state.purpose", panel)
        for purpose in ("test", "review", "accept"):
            self.assertIn(purpose, source)

    def test_job_ttl_is_24_hours(self):
        source = js_source("bridge-api.js")
        self.assertIn("JOB_TTL_MS = 24 * 60 * 60 * 1000", source)
        panel = js_source("sidepanel.js")
        self.assertIn("pruneJobs", panel)
        self.assertIn("created_at", panel)

    def test_red_texts_the_card_requires(self):
        panel = js_source("sidepanel.js")
        self.assertIn("请重新配对", panel)
        self.assertIn("任务已丢失", panel)
        self.assertIn('data-tone="error"', (EXT / "styles.css").read_text(encoding="utf-8"))

    def test_polling_has_no_set_interval(self):
        for name, source in all_js().items():
            self.assertNotIn("setInterval(", source, name)
        panel = js_source("sidepanel.js")
        self.assertIn('from "./poll-scheduler.js"', panel)
        self.assertEqual(panel.count("new PollScheduler("), 1)

    def test_panel_restores_unfinished_jobs_on_open(self):
        panel = js_source("sidepanel.js")
        self.assertIn("JOBS_KEY", panel)
        self.assertIn("await tick();", panel)          # 面板重开先补一轮
        self.assertIn("poll.start()", panel)

    def test_every_dom_id_used_by_the_panel_exists(self):
        html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
        js = js_source("sidepanel.js")
        ids = set(re.findall(r'id="([^"]+)"', html))
        used = set(re.findall(r'\$\("([^"]+)"\)', js))
        self.assertEqual(sorted(used - ids), [])        # 引用了 HTML 里没有的 id 就会跑崩
        # 模板拼出来的 id 也要在（"mode-${mode}" / "tab-${tab}" / PURPOSES 的 elementId）
        concrete = {"mode-discuss", "mode-debate", "tab-summary", "tab-opinions",
                    "tab-disagreements", "purpose-test", "purpose-review", "purpose-accept"}
        self.assertTrue(concrete <= ids, sorted(concrete - ids))

    def test_submit_kicks_the_poll_immediately(self):
        panel = js_source("sidepanel.js")
        self.assertIn("poll.kick()", panel)
        self.assertIn("kick()", js_source("poll-scheduler.js"))

    def test_html_loads_module_script_and_no_inline_handlers(self):
        html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
        self.assertIn('<script type="module" src="sidepanel.js"></script>', html)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html))
        for handler in ("onclick=", "onload=", "onerror="):
            self.assertNotIn(handler, html)

    def test_html_has_no_remote_resources(self):
        html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
        for scheme in ("http://", "https://", "//cdn"):
            self.assertNotIn(scheme, html)


# ================================================================ 3. 后端 Origin / token
def make_root(tmp: Path) -> Path:
    (tmp / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "tiers.json", tmp / "docs" / "tiers.json")
    return tmp


class OriginCase(unittest.TestCase):
    config = {"allowed_extension_id": EXT_ID}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))
        self.engine = server.Engine(root=self.root, token=TOKEN, port=0,
                                    config=dict(self.config), runner=lambda *a, **k: {})
        self.httpd = server.create_server(0, self.engine)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method: str, path: str, *, token: str | None = TOKEN,
                origin: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            body = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, body, dict(response.getheaders())
        finally:
            conn.close()


class OriginTests(OriginCase):
    def test_correct_origin_with_token_is_200(self):
        status, body, headers = self.request("GET", "/endpoints", origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(headers.get("Vary"), "Origin")

    def test_other_extension_id_is_403(self):
        status, body, _ = self.request("GET", "/endpoints", origin=OTHER_ORIGIN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_origin")
        self.assertIn(EXT_ID, body["message"])

    def test_origin_prefix_lookalike_is_403(self):
        for origin in (f"{ORIGIN}.evil.example", f"{ORIGIN}/", ORIGIN + "extra",
                       "chrome-extension://", "null", "http://127.0.0.1:18765"):
            with self.subTest(origin=origin):
                status, body, _ = self.request("GET", "/endpoints", origin=origin)
                self.assertEqual(status, 403)
                self.assertEqual(body["error"], "bad_origin")

    def test_origin_compare_is_case_insensitive(self):
        status, _, _ = self.request("GET", "/endpoints", origin=ORIGIN.upper())
        self.assertEqual(status, 200)

    def test_no_origin_with_token_is_allowed(self):
        status, body, headers = self.request("GET", "/endpoints")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_no_origin_without_token_is_401(self):
        status, body, headers = self.request("GET", "/endpoints", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))

    def test_correct_origin_without_token_is_401(self):
        status, body, _ = self.request("GET", "/endpoints", token=None, origin=ORIGIN)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_options_preflight_for_the_fixed_id(self):
        status, _, headers = self.request("OPTIONS", "/ask", token=None, origin=ORIGIN)
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("Content-Type", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))

    def test_options_wrong_origin_is_403(self):
        status, body, _ = self.request("OPTIONS", "/ask", token=None, origin=OTHER_ORIGIN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_origin")

    def test_options_without_origin_still_allowed(self):
        status, _, headers = self.request("OPTIONS", "/ask", token=None)
        self.assertEqual(status, 204)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_health_is_minimal(self):
        status, body, _ = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "version"})
        self.assertEqual(body["version"], "0.0.9")
        self.assertEqual(set(body), {"ok", "version"})
        for leaked in ("pid", "uptime", "active_jobs", "name", "token"):
            self.assertNotIn(leaked, json.dumps(body))

    def test_health_without_origin_and_with_wrong_origin(self):
        status, _, headers = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, body, _ = self.request("GET", "/health", token=None, origin=OTHER_ORIGIN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_origin")

    def test_token_in_url_is_rejected(self):
        status, body, _ = self.request("GET", "/endpoints?token=" + TOKEN, origin=ORIGIN)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "token_in_url")

    def test_compare_digest_is_actually_called(self):
        with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            status, _, _ = self.request("GET", "/endpoints", origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(spy.call_count, 1)

    def test_wrong_token_is_401_even_with_correct_origin(self):
        status, body, _ = self.request("GET", "/endpoints", token="nope", origin=ORIGIN)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_non_ascii_token_is_401_not_500(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/endpoints", headers={
                "Authorization": "Bearer töken", "Origin": ORIGIN})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
        finally:
            conn.close()

    def test_paired_request_path_end_to_end(self):
        status, body, _ = self.request("GET", "/endpoints", origin=ORIGIN)
        self.assertEqual(status, 200)
        names = [row["name"] for row in body["endpoints"]]
        self.assertIn("grok", names)
        self.assertEqual(body["purposes"], ["test", "review", "accept"])
        self.assertNotIn(TOKEN, json.dumps(body))


class OriginConfigTests(unittest.TestCase):
    def make(self, value):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return server.Engine(root=Path(tmp.name), token=TOKEN, port=0,
                             config={"allowed_extension_id": value},
                             runner=lambda *a, **k: {})

    def test_engine_builds_the_expected_origin(self):
        self.assertEqual(self.make(EXT_ID).allowed_origin, ORIGIN)
        self.assertEqual(self.make(ORIGIN).allowed_origin, ORIGIN)
        self.assertEqual(self.make(" " + EXT_ID.upper() + " ").allowed_origin, ORIGIN)

    def test_unconfigured_engine_falls_back_to_prefix(self):
        engine = self.make("")
        self.assertEqual(engine.allowed_origin, "")
        self.assertTrue(server.BridgeHandler._origin_allowed("chrome-extension://xyz", ""))
        self.assertFalse(server.BridgeHandler._origin_allowed("https://evil.example", ""))

    def test_invalid_id_warns_and_falls_back(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            engine = self.make("not-an-extension-id")
        self.assertEqual(engine.allowed_origin, "")
        self.assertIn("allowed_extension_id", buffer.getvalue())

    def test_origin_matcher(self):
        match = server.BridgeHandler._origin_allowed
        self.assertTrue(match("", ORIGIN))                       # 没 Origin 放行
        self.assertTrue(match(ORIGIN, ORIGIN))
        self.assertTrue(match("  " + ORIGIN + "  ", ORIGIN))
        self.assertFalse(match("  " + ORIGIN + "/  ", ORIGIN))   # 多一个 / 都不算精确
        self.assertFalse(match(OTHER_ORIGIN, ORIGIN))
        self.assertFalse(match(ORIGIN + "extra", ORIGIN))

    def test_normalize_extension_id(self):
        self.assertEqual(server.normalize_extension_id(EXT_ID), EXT_ID)
        self.assertEqual(server.normalize_extension_id(ORIGIN), EXT_ID)
        self.assertEqual(server.normalize_extension_id(EXT_ID + "/"), EXT_ID)
        for bad in ("", None, "xyz", "a" * 31, "a" * 33, "z" * 32, "chrome-extension://"):
            self.assertEqual(server.normalize_extension_id(bad), "", bad)


# ================================================================ 4. token 文件 ACL
class StateAclTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"BRIDGE_SERVER_STATE": str(self.dir),
                                           "USERNAME": "tester"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_icacls_restricts_to_the_current_user(self):
        with mock.patch.object(server.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            path = server.save_state({"schema_version": 1, "token": TOKEN, "port": 18765})
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "icacls")
        self.assertEqual(argv[1], str(path))
        self.assertIn("/inheritance:r", argv)
        self.assertIn("/grant:r", argv)
        self.assertIn("tester:F", argv)
        self.assertTrue(path.exists())

    def test_icacls_failure_only_warns(self):
        buffer = io.StringIO()
        with mock.patch.object(server.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=5, stdout="denied", stderr="")
            with redirect_stderr(buffer):
                path = server.save_state({"token": TOKEN, "port": 18765})
        self.assertTrue(path.exists())
        self.assertIn("icacls exit=5", buffer.getvalue())
        self.assertEqual(read_json(path)["token"], TOKEN)

    def test_icacls_missing_does_not_raise(self):
        buffer = io.StringIO()
        with mock.patch.object(server.subprocess, "run", side_effect=OSError("no icacls")):
            with redirect_stderr(buffer):
                path = server.save_state({"token": TOKEN, "port": 18765})
        self.assertTrue(path.exists())
        self.assertIn("icacls unavailable", buffer.getvalue())

    def test_empty_username_skips_icacls(self):
        with mock.patch.dict(os.environ, {"USERNAME": ""}):
            buffer = io.StringIO()
            with mock.patch.object(server.subprocess, "run") as run:
                with redirect_stderr(buffer):
                    server.save_state({"token": TOKEN, "port": 18765})
            run.assert_not_called()
        self.assertIn("USERNAME empty", buffer.getvalue())

    def test_harden_is_false_for_missing_file(self):
        self.assertFalse(server.harden_state_acl(self.dir / "nope.json"))


# ================================================================ 5. 轮询单例（node）
NODE = shutil.which("node")
HARNESS = r"""
const moduleUrl = process.argv[2];
const { PollScheduler } = await import(moduleUrl);
const pending = new Map();
let nextId = 1;
const setTimer = (fn) => { const id = nextId++; pending.set(id, fn); return id; };
const clearTimer = (id) => { pending.delete(id); };
let ticks = 0;
let release = null;
const scheduler = new PollScheduler(() => new Promise((resolve) => { ticks += 1; release = resolve; }),
  { interval: 5, setTimer, clearTimer });
const out = {};
out.firstStart = scheduler.start();
out.secondStart = scheduler.start();
out.thirdStart = scheduler.start();
out.pendingAfterStarts = pending.size;
const fire = () => { const entry = [...pending.entries()][0]; pending.delete(entry[0]); entry[1](); };
fire();
await new Promise((r) => setTimeout(r, 5));
out.ticksWhileBusy = ticks;
out.startWhileBusy = scheduler.start();
out.pendingWhileBusy = pending.size;
release();
await new Promise((r) => setTimeout(r, 10));
out.ticksAfterRelease = ticks;
out.pendingAfterRelease = pending.size;
scheduler.stop();
out.pendingAfterStop = pending.size;
out.runningAfterStop = scheduler.running;
scheduler.stop();
out.pendingAfterStopTwice = pending.size;
out.startAfterStop = scheduler.start();
out.pendingAfterRestart = pending.size;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node 不在 PATH，跳过轮询单例的运行时验证（静态断言仍在 ExtensionCodeTests）")
class PollSchedulerTests(unittest.TestCase):
    def test_start_is_idempotent_and_stop_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.mjs"
            harness.write_text(HARNESS, encoding="utf-8")
            module_url = (EXT / "poll-scheduler.js").as_uri()
            proc = subprocess.run([NODE, str(harness), module_url],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(result["firstStart"])
        self.assertFalse(result["secondStart"])
        self.assertFalse(result["thirdStart"])
        self.assertEqual(result["pendingAfterStarts"], 1)
        self.assertEqual(result["ticksWhileBusy"], 1)
        self.assertFalse(result["startWhileBusy"])
        self.assertLessEqual(result["pendingWhileBusy"], 1)
        self.assertEqual(result["ticksAfterRelease"], 1)
        self.assertEqual(result["pendingAfterRelease"], 1)
        self.assertEqual(result["pendingAfterStop"], 0)
        self.assertFalse(result["runningAfterStop"])
        self.assertEqual(result["pendingAfterStopTwice"], 0)
        self.assertTrue(result["startAfterStop"])
        self.assertEqual(result["pendingAfterRestart"], 1)


# ================================================================ 6. 文档
class DocsTests(unittest.TestCase):
    def test_readme_describes_009_side_panel(self):
        readme = (EXT / "README.md").read_text(encoding="utf-8")
        self.assertIn("0.0.9", readme)
        self.assertNotIn("空壳", readme)
        self.assertIn("sidepanel.html", readme)
        self.assertIn(EXT_ID, readme)
        self.assertIn("配对", readme)

    def test_config_note_mentions_the_pairing_rule(self):
        config = read_json(CI)
        self.assertIn("allowed_extension_id", config["note"])
        self.assertEqual(config["port"], 18765)

    def test_server_module_has_no_hardcoded_origin_prefix_only(self):
        source = (SRC / "qi" / "server.py").read_text(encoding="utf-8")
        self.assertIn("EXTENSION_ORIGIN", source)
        self.assertIn("normalize_extension_id", source)
        self.assertIn("harden_state_acl", source)


if __name__ == "__main__":
    unittest.main()
