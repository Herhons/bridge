"""Configuration & model-policy loader for Bridge v0.2.4."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
RUNS_DIR = ROOT / "runs"
TEMPLATES_DIR = ROOT / "assets" / "templates"

DEFAULT_BRIDGE_CONFIG: dict[str, Any] = {
    "version": "0.2.4",
    "host": "127.0.0.1",
    "port": 18765,
    "default_timeout": 180.0,
    "poll_interval": 0.5,
    "stable_seconds": 2.5,
    "lock_timeout": 120.0,
    "chrome_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "headless_default": True,
}

DEFAULT_FREE_MARKERS = (
    "0.00x",
    "限时免费",
    "免费",
    "free",
    "4 hydrogen",
    "日常帮助",
)

DEFAULT_PAID_MULTIPLIER_RE = r"(?<!\d)(?:[1-9]\d*\.\d+|0\.(?:0[1-9]|[1-9]\d*))x"


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_bridge_config(root: Path | None = None) -> dict[str, Any]:
    base = Path(root) if root else ROOT
    cfg = dict(DEFAULT_BRIDGE_CONFIG)
    cfg.update(load_json(base / "config" / "bridge.json"))
    env_port = os.environ.get("BRIDGE_PORT")
    if env_port and env_port.isdigit():
        cfg["port"] = int(env_port)
    return cfg


def load_endpoints_config(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = Path(root) if root else ROOT
    data = load_json(base / "config" / "endpoints.json")
    return dict(data.get("endpoints") or {})


def get_endpoint_config(name: str, root: Path | None = None) -> dict[str, Any]:
    endpoints = load_endpoints_config(root=root)
    if name not in endpoints:
        raise KeyError(f"Unknown endpoint: {name!r}. Available: {sorted(endpoints)}")
    return dict(endpoints[name])


def load_model_policy(root: Path | None = None) -> dict[str, Any]:
    base = Path(root) if root else ROOT
    return load_json(base / "config" / "model_policy.json")


def resolve_profile(
    endpoint: str,
    profile: str | None,
    *,
    policy: dict[str, Any] | None = None,
) -> tuple[str | None, bool]:
    """Resolve ``(model_name, free_only)`` for an endpoint & profile (test/routine/council)."""
    if not profile:
        return None, False
    pol = policy if policy is not None else load_model_policy()
    prof_key = profile.strip().lower()
    prof_meta = (pol.get("profiles") or {}).get(prof_key)
    if prof_meta is None:
        raise ValueError(f"Unknown profile: {profile!r}. Available: {sorted((pol.get('profiles') or {}).keys())}")
    ep_map = (pol.get("endpoints") or {}).get(endpoint.strip().lower()) or {}
    target_model = ep_map.get(prof_key)
    free_only = bool(prof_meta.get("free_only", False))
    return (str(target_model) if target_model else None), free_only


def classify_model_label(label: str, *, policy: dict[str, Any] | None = None) -> str:
    """Classify a UI model label as ``'free'``, ``'paid'``, or ``'unknown'``."""
    text = (label or "").strip()
    if not text:
        return "unknown"
    pol = policy if policy is not None else load_model_policy()
    guard = pol.get("cost_guard") or {}
    paid_pattern = str(guard.get("paid_multiplier_regex") or DEFAULT_PAID_MULTIPLIER_RE)
    if re.search(paid_pattern, text, flags=re.IGNORECASE):
        return "paid"
    low = text.lower()
    free_markers = guard.get("free_markers") or DEFAULT_FREE_MARKERS
    if any(str(m).lower() in low for m in free_markers):
        return "free"
    return "unknown"
