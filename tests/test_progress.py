"""任务卡 01：progress.json 读写 + notify_done 的离线测试。

不连外网：通知成功/失败都打到 127.0.0.1 上的临时假服务器（端口随机、用完即关）。
"""

from offline_guard import ensure_offline
ensure_offline()

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE / "src" / "scripts"))

import notify_done                      # noqa: E402
from qi import progress                 # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        self.server.calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(self.server.code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class FakeHook:
    """本地假通知接收端，记录收到的请求。"""

    def __init__(self, code=200):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.code = code
        self.server.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    @property
    def calls(self):
        return self.server.calls


class ProgressFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_creates_record(self):
        record = progress.write("01", "done", "任务卡 01 做完", root=self.root)
        self.assertEqual(record["card_id"], "01")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["note"], "任务卡 01 做完")
        self.assertRegex(record["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")

        raw = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(raw, record)
        self.assertEqual(set(raw), {"card_id", "status", "note", "updated_at"})

        stamp = datetime.fromisoformat(raw["updated_at"])
        self.assertEqual(stamp.utcoffset(), timedelta(hours=8))

    def test_overwrite_replaces_previous(self):
        progress.write("01", "running", "开工", root=self.root)
        progress.mark_notify_failed(progress.write("01", "done", "第一版", root=self.root), root=self.root)
        self.assertTrue(progress.read(root=self.root)["notify_failed"])

        progress.write("02", "stuck", "等红莉栖确认", root=self.root)
        current = progress.read(root=self.root)
        self.assertEqual((current["card_id"], current["status"]), ("02", "stuck"))
        self.assertEqual(current["note"], "等红莉栖确认")
        self.assertNotIn("notify_failed", current)       # 新一条不该继承上一条的失败标记

        text = (self.root / "progress.json").read_text(encoding="utf-8")
        self.assertEqual(len(json.loads(text)), 4)       # 整份覆盖，不是追加

    def test_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            progress.write("01", "finished", "错状态", root=self.root)
        self.assertFalse((self.root / "progress.json").exists())

    def test_note_is_one_line_and_capped(self):
        record = progress.write("01", "done", "第一行\n第二行\t带制表符 " + "很长" * 200, root=self.root)
        self.assertNotIn("\n", record["note"])
        self.assertNotIn("\t", record["note"])
        self.assertLessEqual(len(record["note"]), progress.NOTE_LIMIT)
        self.assertTrue(record["note"].startswith("第一行 第二行"))

    def test_read_missing_or_broken_returns_empty(self):
        self.assertEqual(progress.read(root=self.root), {})
        (self.root / "progress.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(progress.read(root=self.root), {})


class NotifyScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, argv, env):
        with patch.object(progress, "ROOT", self.root), \
             patch.dict(os.environ, env), \
             patch.object(notify_done, "RETRY_WAIT", 0), \
             patch("builtins.print"):
            return notify_done.main(argv)

    def test_without_url_only_writes_file(self):
        with patch.object(notify_done, "_open",
                          side_effect=AssertionError("不该联网")) as opener:
            code = self.run_main(["--card", "01", "--status", "done", "--note", "无地址"],
                                 {"BRIDGE_NOTIFY_URL": "", "BRIDGE_NOTIFY_KEY": ""})
        opener.assert_not_called()
        self.assertEqual(code, 0)
        record = progress.read(root=self.root)
        self.assertEqual((record["card_id"], record["status"]), ("01", "done"))
        self.assertNotIn("notify_failed", record)

    def test_posts_payload_with_key(self):
        secret = "k-0123456789"
        with FakeHook() as hook:
            code = self.run_main(
                ["--card", "01", "--status", "done", "--note", "发出去了"],
                {"BRIDGE_NOTIFY_URL": hook.url, "BRIDGE_NOTIFY_KEY": secret},
            )
            calls = list(hook.calls)

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/hook")
        self.assertEqual(calls[0]["headers"].get("X-Bridge-Key"), secret)
        self.assertIn("application/json", calls[0]["headers"].get("Content-Type", ""))

        record = progress.read(root=self.root)
        self.assertEqual(calls[0]["body"], record)
        self.assertEqual(record["note"], "发出去了")
        self.assertNotIn("notify_failed", record)
        self.assertNotIn(secret, (self.root / "progress.json").read_text(encoding="utf-8"))

    def test_notify_failure_retries_then_flags(self):
        with FakeHook(code=500) as hook:
            code = self.run_main(
                ["--card", "01", "--status", "done", "--note", "通知会失败"],
                {"BRIDGE_NOTIFY_URL": hook.url, "BRIDGE_NOTIFY_KEY": ""},
            )
            attempts = len(hook.calls)

        self.assertEqual(attempts, notify_done.RETRIES + 1)   # 首发 + 重试 2 次
        self.assertEqual(code, 0)                             # 通知失败不影响退出码
        record = progress.read(root=self.root)
        self.assertTrue(record["notify_failed"])
        self.assertEqual((record["card_id"], record["status"]), ("01", "done"))
        self.assertEqual(record["note"], "通知会失败")

    def test_bad_status_rejected_without_touching_file(self):
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                self.run_main(["--card", "01", "--status", "finished", "--note", "x"],
                              {"BRIDGE_NOTIFY_URL": ""})
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.root / "progress.json").exists())


if __name__ == "__main__":
    unittest.main()
