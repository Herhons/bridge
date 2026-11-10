
from offline_guard import ensure_offline
ensure_offline()

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "src" / "chrome-extension"
sys.path.insert(0, str(ROOT / "src"))

from qi import server  # noqa: E402

TOKEN = "t" * 43


def panel() -> str:
    return (EXT / "sidepanel.js").read_text(encoding="utf-8")


def api() -> str:
    return (EXT / "bridge-api.js").read_text(encoding="utf-8")


def html() -> str:
    return (EXT / "sidepanel.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. F4 接线
class WorkflowWiringTests(unittest.TestCase):
    def test_api_exposes_workflow_and_move(self):
        source = api()
        self.assertIn("export function workflow(", source)
        self.assertIn("export function moveCard(", source)
        self.assertIn("/workflow?file=", source)

    def test_panel_imports_both(self):
        source = panel()
        self.assertIn("moveCard", source)
        self.assertIn("workflow as fetchWorkflow", source)

    def test_only_test_file_can_be_written(self):
        """**三重防护**的关键：`workflowFile()` 只在测试模式返回文件名。"""
        source = panel()
        self.assertIn('const WORKFLOW_TEST_FILE = "progress-test.json";', source)
        body = re.search(r"function workflowFile\(\) \{(.*?)\n\}", source, re.S).group(1)
        self.assertIn("testMode ? WORKFLOW_TEST_FILE : null", body)

    def test_assert_writable_rejects_anything_but_the_test_file(self):
        source = panel()
        body = re.search(r"function assertWritable\(file\) \{(.*?)\n\}",
                         source, re.S).group(1)
        self.assertIn("!canWrite()", body)
        self.assertIn("file !== WORKFLOW_TEST_FILE", body)
        self.assertIn("throw new Error", body)

    def test_move_calls_assert_writable_before_posting(self):
        source = panel()
        body = re.search(r"async function moveWorkflowCard\(card, to\) \{(.*?)\n\}",
                         source, re.S).group(1)
        self.assertIn("assertWritable(file)", body)
        self.assertLess(body.index("assertWritable(file)"), body.index("moveCard(token"))

    def test_optimistic_lock_compares_mtime_before_writing(self):
        source = panel()
        body = re.search(r"async function moveWorkflowCard\(card, to\) \{(.*?)\n\}",
                         source, re.S).group(1)
        self.assertIn("fresh.mtime !== workflowMirror.mtime", body)

    def test_tick_refreshes_workflow_within_the_existing_poll(self):
        """≤10s 靠复用 3 秒轮询实现，**不新增定时器**。"""
        body = re.search(r"async function tick\(\) \{(.*?)\n\}", panel(), re.S).group(1)
        self.assertIn("await loadWorkflow()", body)

    def test_workflow_mirror_is_persisted_for_reload(self):
        body = re.search(r"async function loadWorkflow\(\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        self.assertIn("writeLocal(WORKFLOW_KEY", body)

    def test_storage_change_listens_to_three_keys(self):
        body = re.search(r"chrome\.storage\.onChanged\?\.addListener\((.*?)\n  \}\);",
                         panel(), re.S).group(1)
        for key in ("TEST_KEY", "WORKFLOW_KEY", "ERRORS_KEY", "DRAFT_KEY"):
            with self.subTest(key=key):
                self.assertIn(key, body)


# ---------------------------------------------------------------- 2. F5 去重
class ErrorDedupTests(unittest.TestCase):
    def test_error_key_is_three_dimensional_and_text_free(self):
        body = re.search(r"function errorKey\(jobId, endpoint, error\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        self.assertIn("jobId", body)
        self.assertIn("endpoint", body)
        self.assertIn("error.code || error.kind", body)
        self.assertNotIn("error.message", body, "去重键里不许含文案（文案会变）")

    def test_show_error_counts_instead_of_appending(self):
        body = re.search(r"function showError\(jobId, endpoint, error, target\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        self.assertIn("errorSeen.get(key)", body)
        self.assertIn("errorSeen.set(key, seen)", body)
        self.assertIn("×${seen}", body)

    def test_poll_job_handles_all_five_kinds(self):
        body = re.search(r"async function pollJob\(entry\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        for kind in ("not_found", "unauthorized", "offline"):
            with self.subTest(kind=kind):
                self.assertIn(f'"{kind}"', body)
        # bad_request / server 走 else 分支：必须有兜底，不许像改造前那样静默
        self.assertIn("showError(", body)
        self.assertIn("else {", body, "bad_request / server 必须有兜底分支")

    def test_unauthorized_and_offline_are_throttled(self):
        body = re.search(r"async function pollJob\(entry\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        self.assertIn("=== 1) {", body, "只有第一次才播报，否则每 3 秒刷一次")


# ---------------------------------------------------------------- 3. 终态回填
class EnsureJobTests(unittest.TestCase):
    def test_ensure_job_exists_and_is_guarded_by_a_set(self):
        body = re.search(r"async function ensureJob\(entry\) \{(.*?)\n\}",
                         panel(), re.S).group(1)
        self.assertIn("jobCache.has(entry.job_id)", body)
        self.assertIn("fetchedJobs.has(entry.job_id)", body)
        self.assertIn("fetchedJobs.add(entry.job_id)", body)

    def test_initialize_backfills_the_selected_job(self):
        body = re.search(r"async function initialize\(\) \{(.*?)\n  poll\.start\(\);",
                         panel(), re.S).group(1)
        self.assertIn("renderWorkflow()", body)
        self.assertIn("await loadWorkflow()", body)


# ---------------------------------------------------------------- 4. 静态约束（不该破的）
class PreservedInvariantsTests(unittest.TestCase):
    def test_background_js_is_untouched(self):
        source = (EXT / "background.js").read_text(encoding="utf-8")
        for banned in ("addListener", "setInterval", "setTimeout", "chrome.tabs",
                       "chrome.storage", "chrome.scripting"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        self.assertIn("openPanelOnActionClick: true", source)

    def test_no_console_in_any_js(self):
        for path in EXT.glob("*.js"):
            with self.subTest(file=path.name):
                self.assertNotIn("console.", path.read_text(encoding="utf-8"))

    def test_new_dom_ids_exist(self):
        ids = set(re.findall(r'id="([^"]+)"', html()))
        for wanted in ("workflow-mode", "workflow-status", "workflow-list"):
            with self.subTest(id=wanted):
                self.assertIn(wanted, ids)

    def test_every_dollar_id_still_exists(self):
        ids = set(re.findall(r'id="([^"]+)"', html()))
        used = set(re.findall(r'\$\("([^"]+)"\)', panel()))
        self.assertEqual(sorted(used - ids), [])

    def test_request_id_is_persisted_on_the_entry(self):
        body = re.search(r"const entry = \{(.*?)\n    \};", panel(), re.S).group(1)
        self.assertIn("request_id: requestId", body)

    def test_single_poll_scheduler_still(self):
        self.assertEqual(panel().count("new PollScheduler("), 1)


# ---------------------------------------------------------------- 5. 状态机一致性
class WorkflowTableTests(unittest.TestCase):
    def test_js_next_table_matches_workflow_json(self):
        """JS 里那张"推进目标"表必须与 workflow.json 的 transitions 首项一致。

        前端只拿它给按钮选默认目标（合法性仍由服务端裁决），但一旦漂移，
        按钮就会指向一个必然 409 的目标——所以这里钉死。
        """
        block = re.search(r"const WORKFLOW_NEXT = \{(.*?)\n\};",
                          panel(), re.S).group(1)
        pairs = {}
        for key, quoted, null in re.findall(r"(\w+):\s*(?:\"(\w+)\"|(null))", block):
            pairs[key] = quoted if quoted else None
        definition = json.loads(
            (ROOT / "docs" / "workflow" / "workflow.json").read_text(encoding="utf-8"))
        for state, targets in definition["transitions"].items():
            with self.subTest(state=state):
                self.assertEqual(pairs.get(state), targets[0] if targets else None,
                                 f"{state} 的推进目标与 workflow.json 不一致")


# ---------------------------------------------------------------- 6. 故障注入（真跑 Engine）
class _NeverRunner:
    """故障注入生效时，runner 一次都不该被调用（没碰 UI）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, timeout, cwd, env=None):
        self.calls.append(cmd)
        return {"ok": True, "timeout": False, "exit_code": 0,
                "stdout": '{"ok": true, "stage": "answered", "reply": "x"}',
                "stderr": "", "elapsed": 0.01}


class FaultInjectionTests(unittest.TestCase):
    CONFIG = {"queue_limit": 4, "choose_timeout": 5.0, "use_timeout": 5.0,
              "endpoint_timeout": 15.0, "ui_lock_timeout": 5.0}

    def make_root(self, tmp: Path) -> Path:
        (tmp / "docs" / "workflow").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "docs" / "tiers.json", tmp / "docs" / "tiers.json")
        shutil.copy2(ROOT / "docs" / "workflow" / "workflow.json",
                     tmp / "docs" / "workflow" / "workflow.json")
        (tmp / "src" / "scripts").mkdir(parents=True, exist_ok=True)
        for suffix in ("choose", "use"):
            (tmp / "src" / "scripts" / f"grok_{suffix}.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")
        (tmp / "runs").mkdir(parents=True, exist_ok=True)
        return tmp

    def run_job(self, config_extra, fault_file=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = self.make_root(Path(self._tmp.name))
        if fault_file is not None:
            (root / "runs" / "test-faults.json").write_text(
                json.dumps(fault_file), encoding="utf-8")
        runner = _NeverRunner()
        config = {**self.CONFIG, **config_extra}
        engine = server.Engine(root=root, token=TOKEN, port=0,
                               config=config, runner=runner)
        job, _ = engine.submit(["grok"], "offline-only", "test")
        finished = engine.wait_job(job["job_id"], 10)
        engine.close() if hasattr(engine, "close") else None
        return finished, runner

    def test_fault_needs_both_conditions(self):
        """只配 allow_test_faults、没有 fault 文件 → 不生效。"""
        job, runner = self.run_job({"allow_test_faults": True})
        self.assertNotEqual(job["endpoints"]["grok"].get("error_code"), "no_input")
        self.assertTrue(runner.calls, "没注入故障时应该真的去跑脚本")

    def test_fault_file_without_the_switch_is_ignored(self):
        """只放 fault 文件、开关是 false → 不生效。生产配置就是这个状态。"""
        job, runner = self.run_job({"allow_test_faults": False},
                                   {"endpoint": "grok", "fault": "no_input"})
        self.assertNotEqual(job["endpoints"]["grok"].get("error_code"), "no_input")

    def test_both_conditions_fail_before_any_ui_action(self):
        job, runner = self.run_job({"allow_test_faults": True},
                                   {"endpoint": "grok", "fault": "no_input"})
        record = job["endpoints"]["grok"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "no_input")
        self.assertEqual(runner.calls, [], "故障注入必须在任何 UI 动作之前生效")

    def test_fault_is_scoped_to_one_endpoint(self):
        job, runner = self.run_job({"allow_test_faults": True},
                                   {"endpoint": "someone-else", "fault": "no_input"})
        self.assertNotEqual(job["endpoints"]["grok"].get("error_code"), "no_input")

    def test_shipped_config_has_the_switch_off(self):
        shipped = json.loads(
            (ROOT / "docs" / "bridge_server.json").read_text(encoding="utf-8"))
        self.assertIs(shipped.get("allow_test_faults"), False,
                      "出货配置必须是 false，否则故障注入会误触发")


if __name__ == "__main__":
    unittest.main()
