"""B15-A：配置归一、按端预算（只许抬高）、单 job 席位数上限。

三条不可退让的约束：
1. `use_timeout` 仍是 120（`test_b1_state_machine.py:366-381` 钉着）；
2. review/accept 的 1500 下限不许被覆盖压低；
3. 配置写错**不许抛**，只忽略并提示 —— 配置坏不该让后端起不来。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import server, tier_guard  # noqa: E402

BASE = server.DEFAULT_CONFIG["use_timeout"]


class BudgetMergeTests(unittest.TestCase):
    def test_override_only_raises(self):
        cfg = {"budget_overrides": {"codex": {"use": 300}}}
        self.assertEqual(server.apply_budgets(cfg, "codex", "test", "use", BASE), 300.0)
        # 别的端不受影响
        self.assertEqual(server.apply_budgets(cfg, "grok", "test", "use", BASE), BASE)

    def test_review_floor_cannot_be_lowered(self):
        cfg = {"budget_overrides": {"grok": {"use": 60}}}
        floor = tier_guard.floor_timeout("review", BASE)
        self.assertEqual(server.apply_budgets(cfg, "grok", "review", "use", floor), floor)

    def test_purpose_specific_key_wins_over_endpoint_key(self):
        cfg = {"budget_overrides": {"codex": {"use": 300}, "codex:test": {"use": 450}}}
        self.assertEqual(server.apply_budgets(cfg, "codex", "test", "use", BASE), 450.0)
        self.assertEqual(server.apply_budgets(cfg, "codex", "review", "use", 1500), 1500)

    def test_ceiling_is_enforced(self):
        cfg = {"budget_overrides": {"*": {"use": 99999}}}
        self.assertEqual(server.apply_budgets(cfg, "codex", "test", "use", BASE),
                         server.BUDGET_CEILING)

    def test_shipped_defaults_are_untouched(self):
        self.assertEqual(BASE, 120.0)
        self.assertEqual(tier_guard.floor_timeout("review", BASE), 1500.0)
        self.assertEqual(tier_guard.floor_timeout("test", BASE), 120.0)


class NormalizeTests(unittest.TestCase):
    def test_bad_values_are_ignored_not_raised(self):
        for bad in ("codex", 5, None, [], {"codex": 300}, {"codex": {"use": "x"}},
                    {"codex": {"use": -5}}, {"nope": {"use": 300}}):
            got = server.normalize_budgets(bad, endpoints=["codex"], purposes=["test"])
            self.assertIsInstance(got, dict, bad)
            self.assertEqual(got, {}, f"应当整条忽略：{bad!r}")

    def test_good_values_survive(self):
        got = server.normalize_budgets(
            {"codex": {"use": 300}, "grok:test": {"use": 180}, "*": {"endpoint": 600}},
            endpoints=["codex", "grok"], purposes=["test"])
        self.assertEqual(got["codex"], {"use": 300.0})
        self.assertEqual(got["grok:test"], {"use": 180.0})
        self.assertEqual(got["*"], {"endpoint": 600.0})

    def test_unknown_kind_is_dropped(self):
        got = server.normalize_budgets({"codex": {"use": 300, "whatever": 9}},
                                       endpoints=["codex"], purposes=["test"])
        self.assertEqual(got, {"codex": {"use": 300.0}})

    def test_name_lists_reject_strings(self):
        self.assertEqual(server.normalize_name_list("dsh"), [])
        self.assertEqual(server.normalize_name_list(["DSH", "dsh", " codex "]),
                         ["dsh", "codex"])

    def test_number_map_ignores_junk(self):
        self.assertEqual(server.normalize_number_map({"codex": 45, "x": -1, "y": "z"}),
                         {"codex": 45.0})


class EndpointCapTests(unittest.TestCase):
    def _engine(self, cap):
        tmp = Path(tempfile.mkdtemp(prefix="b15-cap-"))
        engine = server.Engine(root=tmp, token="fixture", port=0,
                               runner=lambda *a, **k: {})
        engine.config["max_endpoints_per_job"] = cap
        engine.allowed_endpoints = lambda: ["chatgpt", "gemini", "grok", "deepseek",
                                            "codex", "opencode"]
        return engine

    def test_too_many_endpoints_is_rejected_with_a_readable_code(self):
        engine = self._engine(4)
        with self.assertRaises(server.RequestError) as ctx:
            engine.request_ask({"endpoints": ["chatgpt", "gemini", "grok", "deepseek", "codex"],
                                "prompt": "x", "purpose": "test"})
        self.assertEqual(ctx.exception.code, "too_many_endpoints")
        self.assertIn("分两轮", str(ctx.exception))

    def test_exactly_cap_is_allowed(self):
        engine = self._engine(4)
        engine.submit = lambda *a, **k: ({"job_id": "j", "status": "queued"}, False)
        job, dup = engine.request_ask(
            {"endpoints": ["chatgpt", "gemini", "grok", "deepseek"],
             "prompt": "x", "purpose": "test"})
        self.assertEqual(job["job_id"], "j")
        self.assertFalse(dup)


class ShippedConfigTests(unittest.TestCase):
    def test_load_config_normalizes_new_keys(self):
        cfg = server.load_config(ROOT)
        self.assertIn(cfg["web_window_mode"], ("tabs", "per_site"))
        self.assertIsInstance(cfg["budget_overrides"], dict)
        self.assertIsInstance(cfg["stall_busy_ignore"], dict)
        self.assertIsInstance(cfg["prelaunch_endpoints"], list)
        self.assertGreaterEqual(int(cfg["max_endpoints_per_job"]), 1)

    def test_broken_json_falls_back_to_defaults(self):
        tmp = Path(tempfile.mkdtemp(prefix="b15-bad-"))
        (tmp / "docs").mkdir()
        (tmp / "docs" / "bridge_server.json").write_text("{ not json", encoding="utf-8")
        cfg = server.load_config(tmp)
        self.assertEqual(cfg["use_timeout"], 120.0)
        self.assertEqual(cfg["web_window_mode"], "per_site")
        self.assertEqual(cfg["budget_overrides"], {})


if __name__ == "__main__":
    unittest.main()
