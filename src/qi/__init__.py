"""v0.0.9 内核：按图找图的 AI 客户端驱动。

    from qi import Screen, Library
    s = Screen()
    s.click_image("ds_composer")
    s.type_text("你好")
    s.click_image("ds_send_on")
"""

from .library import Library
from .screen import DPI_LEVEL, LIBRARY, ForegroundLost, Match, Screen

__all__ = ["Screen", "Match", "Library", "ForegroundLost", "DPI_LEVEL", "LIBRARY"]

# --- 收口后的旧模块名兼容映射（轻量 sys.meta_path，零 I/O 副作用） ---
# 「1 个总控 + 8 个 AI 独立脚本」收口后，下列旧模块已合并：
#   qi.desk_sites.SITES          -> qi.desk_flow.SITES
#   qi.drivers.SiteDriver        -> qi.flow.SiteDriver
#   scripts.<ep>_choose/_use     -> scripts.<ep> 的 choose/use 子空间
#   scripts.<原总控子模块>        -> scripts.bridge 的子命令模块
# 这里只做 import 级转发：不 patch builtins.open / io.open / pathlib.Path.exists，
# 不依赖任何本地归档目录，生产运行时没有全局副作用。
import importlib as _importlib
import importlib.abc as _abc
import importlib.util as _util
import sys as _sys


class _AliasLoader(_abc.Loader):
    """把旧模块名解析为新模块里的对应对象。"""

    def __init__(self, fullname: str, resolver):
        self.fullname = fullname
        self.resolver = resolver

    def create_module(self, spec):
        mod = self.resolver()
        _sys.modules[self.fullname] = mod
        return mod

    def exec_module(self, module):
        pass


class _ConsolidatedFinder(_abc.MetaPathFinder):
    _ENDPOINTS = frozenset({
        "chatgpt", "gemini", "grok", "deepseek",
        "codex", "dsh", "opencode", "workbuddy",
    })
    _BRIDGE_SUBMODS = frozenset({
        "bridge_server", "workflow_use", "notify_done",
        "workbuddy_autopush", "live_job", "iterate",
    })

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "qi.desk_sites":
            return _util.spec_from_loader(
                fullname, _AliasLoader(
                    fullname, lambda: _importlib.import_module("qi.desk_flow")))
        if fullname == "qi.drivers":
            return _util.spec_from_loader(
                fullname, _AliasLoader(
                    fullname, lambda: _importlib.import_module("qi.flow")))

        for prefix in ("scripts.", "src.scripts.", ""):
            if not fullname.startswith(prefix):
                continue
            rest = fullname[len(prefix):]

            if rest in self._BRIDGE_SUBMODS:
                def _bridge(rest=rest):
                    return _importlib.import_module("scripts.bridge").get_subcommand_module(rest)
                return _util.spec_from_loader(
                    fullname, _AliasLoader(fullname, _bridge))

            for ep in self._ENDPOINTS:
                for suffix, getter in (("_choose", "get_choose_module"),
                                       ("_use", "get_use_module")):
                    if rest == ep + suffix:
                        def _subspace(ep=ep, getter=getter):
                            parent = _importlib.import_module(f"scripts.{ep}")
                            fn = getattr(parent, getter, None)
                            return fn() if fn is not None else parent
                        return _util.spec_from_loader(
                            fullname, _AliasLoader(fullname, _subspace))
        return None


if not any(type(f).__name__ == "_ConsolidatedFinder" for f in _sys.meta_path):
    _sys.meta_path.insert(0, _ConsolidatedFinder())
