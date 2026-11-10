"""Default bootstrap for literal unittest discovery and individual modules."""
import importlib.util
import sys
from pathlib import Path


def ensure_offline():
    root = Path(__file__).resolve().parents[2]
    active = getattr(sys, "_bridge_offline_guard_active", None)
    if active is not None:
        if Path(active) != root:
            raise RuntimeError("offline guard belongs to another checkout")
        return sys._bridge_offline_guard_module
    name = "_bridge_unittest_offline_sitecustomize"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("sitecustomize.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if getattr(sys, "_bridge_offline_guard_active", None) != str(root):
        raise RuntimeError("offline guard did not activate")
    return module


def expected_denial(kind):
    return ensure_offline().expected_denial(kind)
