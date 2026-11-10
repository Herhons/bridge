
from offline_guard import ensure_offline
ensure_offline()

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class WholeResponseDeadline(unittest.TestCase):
    def test_real_js_api_body_deadlines_classification_scheduler_and_initialize(self):
        root = Path(__file__).resolve().parents[1]
        node = shutil.which("node")
        self.assertTrue(node, "Node required for actual JS deadline regression")
        ext = root / "src/chrome-extension"
        result = subprocess.run([node, str(root / "tests/health_deadline_harness.mjs"),
                                 (ext / "bridge-api.js").as_uri(),
                                 (ext / "poll-scheduler.js").as_uri(), str(ext / "sidepanel.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["deadlineCases"], 5)
        self.assertGreaterEqual(result["pollTicks"], 2)
        self.assertTrue(result["initializeHealthConcurrent"])
