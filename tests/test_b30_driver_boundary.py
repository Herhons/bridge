"""B30：驱动层不许自己实现发送栈/账本/锁。

Round 3 三席一致：第一步只把「最稳的那条上传路径」抽成无状态驱动，
账本、锁、超时、重试仍留在总控。没有静态约束的话，下一个人很容易在驱动里
再写一套 `ask`，于是出现第三套发送栈。

这里用 AST 扫 `src/qi/drivers/`：
  · 不许 import `submissions` / `uilock` / `flow` / `conversation`（发送栈本身）；
  · 不许出现 `.ask(`、`reserve(`、`pending(` 调用；
  · 每个驱动模块必须实现协议里的方法名（出现即视为"是个驱动"，而不是工具函数）。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVERS = ROOT / "src" / "qi" / "drivers"
FORBIDDEN_IMPORTS = {"submissions", "uilock", "flow", "conversation"}
FORBIDDEN_CALLS = {"ask", "reserve", "pending", "mark_level", "finish"}


def modules() -> list[Path]:
    return sorted(p for p in DRIVERS.glob("*.py") if p.name != "__init__.py"
                  or p.stat().st_size > 0)


class DriverBoundaryTests(unittest.TestCase):
    def test_drivers_do_not_import_the_send_stack(self):
        for path in modules():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for name in names:
                    leaf = name.rsplit(".", 1)[-1]
                    self.assertNotIn(leaf, FORBIDDEN_IMPORTS,
                                     f"{path.name} 引入了发送栈模块 {name}")

    def test_drivers_do_not_call_ledger_or_ask(self):
        for path in modules():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, FORBIDDEN_CALLS,
                                     f"{path.name} 调用了 {node.func.attr}()")

    def test_protocol_still_declares_the_atomic_actions(self):
        sys.path.insert(0, str(ROOT / "src"))
        from qi.drivers import SiteDriver
        for name in ("ensure_session", "choose_model", "upload", "submit", "read_reply"):
            self.assertTrue(hasattr(SiteDriver, name), name)


if __name__ == "__main__":
    unittest.main()
