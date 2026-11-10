"""UI-BOOTSTRAP-2：锁文件打不开时必须是 ui_busy(8)，不是 traceback。

2026-11-07 03:41 实测：另一个自动化进程正持着 `runs/ui.lock` 时，
`opencode_choose --probe` 在 `_open()` 上抛
`PermissionError: [Errno 13] ... ui.lock`，整条命令以 traceback 收场（退出码 1）。
这不是程序错误，是"UI 被别的自动化占着"：应当打印 `stage=ui_busy` 并退 8，
既不重试抢锁，也绝不绕过锁去点桌面。
"""
from offline_guard import ensure_offline
ensure_offline()

import builtins
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import exit_codes, uilock  # noqa: E402


class LockedFile(unittest.TestCase):
    def test_permission_denied_on_lock_file_is_ui_busy(self):
        real_open = builtins.open
        calls = []

        def fake_open(path, *args, **kwargs):
            name = str(path)
            if name.endswith("ui.lock") or name.endswith("ui.lock.lease"):
                raise PermissionError(13, "拒绝访问", name)
            return real_open(path, *args, **kwargs)

        ran = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("builtins.open", side_effect=fake_open), \
                redirect_stdout(out):
            code = uilock.run("offline-test", lambda: ran.append(1) or 0,
                              timeout=0.1, root=Path(tmp))
        self.assertEqual(code, exit_codes.UI_LOCK_BUSY)
        self.assertEqual(ran, [], "拿不到锁绝不许执行动作")
        payload = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["stage"], "ui_busy")
        self.assertIn("ui lock not openable", payload["error"])
        calls.append(payload)

    def test_missing_directory_permission_is_also_busy(self):
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("ui.lock"):
                raise PermissionError(13, "拒绝访问", str(path))
            return real_open(path, *args, **kwargs)

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("builtins.open", side_effect=fake_open), \
                redirect_stdout(out):
            code = uilock.run("offline-test", lambda: 0, timeout=0.1, root=Path(tmp))
        self.assertEqual(code, exit_codes.UI_LOCK_BUSY)


if __name__ == "__main__":
    unittest.main()
