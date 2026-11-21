"""Model and UI drift detection + structured diagnostic reporting (M4-14).

When a provider introduces new models (e.g. a faster/better Gemini model beyond
Neon), removes an option, or changes UI selectors, Bridge must fail fast with a
structured diagnostic report under ``runs/drift/`` rather than hammering broken
selectors blindly. ``scripts/auto_heal_loop.py`` attaches the latest unacked
drift report to the cloud brain handoff bundle so the brain can issue a config
or driver fix patch.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRIFT_DIR = ROOT / "runs" / "drift"
LATEST_FILENAME = "latest.json"


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _known_model_labels(
    configured_models: dict[str, Any] | list[str] | None,
    configured_aliases: dict[str, Any] | None = None,
) -> set[str]:
    known: set[str] = set()
    if isinstance(configured_models, dict):
        for key, val in configured_models.items():
            if key:
                known.add(_norm_label(str(key)))
            if isinstance(val, str) and val.strip():
                known.add(_norm_label(val))
            elif isinstance(val, dict):
                for field in ("label", "ui_label", "name", "id"):
                    if val.get(field):
                        known.add(_norm_label(str(val[field])))
    elif isinstance(configured_models, (list, tuple, set)):
        for item in configured_models:
            if item:
                known.add(_norm_label(str(item)))

    if isinstance(configured_aliases, dict):
        for key, aliases in configured_aliases.items():
            if key:
                known.add(_norm_label(str(key)))
            if isinstance(aliases, str) and aliases.strip():
                known.add(_norm_label(aliases))
            elif isinstance(aliases, (list, tuple, set)):
                for alias in aliases:
                    if alias:
                        known.add(_norm_label(str(alias)))
    return {item for item in known if item}


def detect_model_drift(
    endpoint: str,
    observed_options: list[str] | tuple[str, ...] | None,
    configured_models: dict[str, Any] | list[str] | None = None,
    configured_aliases: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare live-observed model labels against configured models/aliases."""
    cleaned_observed: list[str] = []
    seen_norm: set[str] = set()
    for raw in observed_options or []:
        text = re.sub(r"\s+", " ", str(raw or "").strip())
        if not text:
            continue
        key = text.lower()
        if key not in seen_norm:
            seen_norm.add(key)
            cleaned_observed.append(text)

    known = _known_model_labels(configured_models, configured_aliases)
    new_models = [opt for opt in cleaned_observed if _norm_label(opt) not in known]

    expected_keys: list[str] = []
    if isinstance(configured_models, dict):
        expected_keys = [str(k) for k in configured_models.keys()]
    elif isinstance(configured_models, (list, tuple)):
        expected_keys = [str(k) for k in configured_models]

    missing_models: list[str] = []
    if cleaned_observed and expected_keys:
        for key in expected_keys:
            candidates = {_norm_label(key)}
            if isinstance(configured_aliases, dict):
                raw_aliases = configured_aliases.get(key)
                if isinstance(raw_aliases, str):
                    candidates.add(_norm_label(raw_aliases))
                elif isinstance(raw_aliases, (list, tuple, set)):
                    candidates.update(_norm_label(str(a)) for a in raw_aliases if a)
            if not (candidates & seen_norm):
                missing_models.append(key)

    drift_detected = bool(new_models or missing_models)
    parts: list[str] = []
    if new_models:
        parts.append("new live models: %s" % ", ".join(new_models))
    if missing_models:
        parts.append("missing configured models: %s" % ", ".join(missing_models))
    reason = "; ".join(parts) if parts else "no model drift detected"

    return {
        "endpoint": endpoint,
        "drift_detected": drift_detected,
        "observed_options": cleaned_observed,
        "new_models": new_models,
        "missing_models": missing_models,
        "reason": reason,
    }


def summarize_ui_controls(nodes: list[Any] | None, *, limit: int = 20) -> list[dict[str, Any]]:
    """Extract a compact, JSON-serializable summary of visible UIA controls."""
    summary: list[dict[str, Any]] = []
    for node in nodes or []:
        if len(summary) >= max(1, int(limit)):
            break
        if isinstance(node, dict):
            role = str(node.get("role") or node.get("control_type") or "")
            name = str(node.get("name") or "")
            rect = node.get("rect")
        else:
            role = str(getattr(node, "role", "") or getattr(node, "control_type", "") or "")
            name = str(getattr(node, "name", "") or "")
            rect = getattr(node, "rect", None)
        if not role and not name:
            continue
        item: dict[str, Any] = {"role": role, "name": name[:120]}
        if rect is not None:
            item["rect"] = list(rect) if isinstance(rect, (list, tuple)) else rect
        summary.append(item)
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_drift_report(
    endpoint: str,
    *,
    kind: str,
    reason: str,
    observed_options: list[str] | None = None,
    new_models: list[str] | None = None,
    missing_models: list[str] | None = None,
    ui_controls: list[Any] | None = None,
    extra: dict[str, Any] | None = None,
    drift_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Persist a structured drift report to runs/drift/ and update latest.json."""
    target_dir = Path(drift_dir) if drift_dir is not None else DEFAULT_DRIFT_DIR
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe_ep = re.sub(r"[^a-zA-Z0-9_-]+", "_", (endpoint or "unknown").strip()) or "unknown"
    safe_kind = re.sub(r"[^a-zA-Z0-9_-]+", "_", (kind or "drift").strip()) or "drift"
    report_path = target_dir / ("%s_%s_%s.json" % (safe_ep, safe_kind, stamp))

    payload: dict[str, Any] = {
        "schema": "bridge.drift.v1",
        "timestamp": (now or datetime.now()).isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "kind": kind,
        "reason": reason,
        "acknowledged": False,
        "observed_options": list(observed_options or []),
        "new_models": list(new_models or []),
        "missing_models": list(missing_models or []),
        "ui_controls": summarize_ui_controls(ui_controls),
        "extra": dict(extra or {}),
        "report_file": str(report_path),
    }
    _atomic_write_json(report_path, payload)
    _atomic_write_json(target_dir / LATEST_FILENAME, payload)
    return report_path


def load_latest_drift_report(
    *,
    drift_dir: Path | None = None,
    only_unacked: bool = True,
) -> dict[str, Any] | None:
    """Read runs/drift/latest.json if present (optionally filtering acknowledged)."""
    target_dir = Path(drift_dir) if drift_dir is not None else DEFAULT_DRIFT_DIR
    latest = target_dir / LATEST_FILENAME
    if not latest.is_file():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if only_unacked and bool(data.get("acknowledged", False)):
        return None
    return data


def format_drift_handoff_section(report: dict[str, Any] | None) -> str:
    """Render a compact Markdown section for cloud-brain handoff bundles."""
    if not report:
        return ""
    lines = [
        "## Model / UI Drift Diagnostic Report",
        "- Endpoint: %s" % (report.get("endpoint") or "unknown"),
        "- Kind: %s" % (report.get("kind") or "unknown"),
        "- Timestamp: %s" % (report.get("timestamp") or ""),
        "- Reason: %s" % (report.get("reason") or ""),
    ]
    if report.get("observed_options"):
        lines.append("- Observed live options: %s" % ", ".join(str(x) for x in report["observed_options"]))
    if report.get("new_models"):
        lines.append("- New unconfigured models: %s" % ", ".join(str(x) for x in report["new_models"]))
    if report.get("missing_models"):
        lines.append("- Missing configured models: %s" % ", ".join(str(x) for x in report["missing_models"]))
    if report.get("ui_controls"):
        preview = "; ".join(
            "%s:%s" % (c.get("role", ""), c.get("name", ""))
            for c in report["ui_controls"][:8]
        )
        lines.append("- Visible UI controls sample: %s" % preview)
    if report.get("report_file"):
        lines.append("- Report file: %s" % report["report_file"])
    return "\n".join(lines) + "\n"
