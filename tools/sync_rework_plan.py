"""Render WIP current status while preserving the original PLAN as history."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

WIP = Path(r"F:\Bridge\v0.0.9_codex_wip")
SOURCE = Path(r"F:\Bridge\v0.0.9")
REPORTS = SOURCE / "evidence/codex_recheck_20261106"
CARDS = ["04c", "PLAN-r1", "GEM", "WEB", "05", "WEB2", "06a", "06b", "06c", "06d",
         "07", "08", "09a", "09b", "09e", "09f-fix2"]
ALIASES = {"09a-lock-integration": "09a", "06a-workbuddy-purpose": "06a",
           "06b-workbuddy-presend": "06b", "06c-composite-tier": "06c"}
WIP_STATES = {
    "04c": "note原因已修并自测；待其他厂商独立验收",
    "PLAN-r1": "本WIP当前表同步；正式PLAN未改",
    "GEM": "保留原复验结论；WIP后续保护改动待独立验收",
    "WEB": "窗口清理已修并自测；历史安装完整命令仍缺",
    "05": "原功能复验通过；WIP补共用并发锁，待独立验收",
    "WEB2": "功能原有测试通过；Grok最低fast同轮原始证据待补",
    "06a": "用途拒发已修并自测；WorkBuddy空表仍拒发，不虚构模型档位",
    "06b": "写后门禁及失败留证已修并自测；待独立验收",
    "06c": "联合回执已修并自测；任一维度不可读拒发；待独立验收",
    "06d": "保留原复验结论；后续WIP入口调整待独立验收",
    "07": "旧回复归属已修并自测；待独立验收",
    "08": "已有CARD-08成品及自动推整合自测；未接真实调度/推送",
    "09a": "父子锁、持久去重、workflow并发均已修并自测；待独立验收",
    "09b": "已有扩展成品；精确health/正文截止已修并自测；未加载Chrome",
    "09e": "六入口自身输入已修并自测；WorkBuddy本人完整重启原始证据待补",
    "09f-fix2": "fix3实际入口恢复已修并自测；未重写旧fix2结论",
}


def render():
    before = (SOURCE / "PLAN.md").read_bytes()
    baseline_path = WIP / "COPY_BASELINE.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(before).hexdigest()
    if "PLAN.md" in baseline["files"]:
        assert baseline["files"]["PLAN.md"] == digest, "Original PLAN drift"
    else:
        baseline["files"]["PLAN.md"] = digest
        baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    original = (SOURCE / "PLAN.md").read_text(encoding="utf-8-sig")
    copied = WIP / "evidence/recheck_sources"
    copied.mkdir(parents=True, exist_ok=True)
    entries = []
    for card in CARDS:
        name = "CARD-" + card + ".txt"
        raw = (REPORTS / name).read_bytes()
        (copied / name).write_bytes(raw)
        text = raw.decode("utf-8-sig")
        verdict = re.search(r"^VERDICT (PASS|FAIL)\r?$", text, re.M).group(1)
        entries.append({"card": card, "recheck_verdict": verdict,
                        "wip_status": WIP_STATES[card], "independent_acceptance": "pending",
                        "source": str(REPORTS / name), "copy": "evidence/recheck_sources/" + name,
                        "sha256": hashlib.sha256(raw).hexdigest()})
    summary = (REPORTS / "SUMMARY.txt").read_bytes()
    (copied / "SUMMARY.txt").write_bytes(summary)
    index = (SOURCE / "cards/drafts_rework_20261106/INDEX.md").read_text(encoding="utf-8")
    order = re.findall(r"\| \d+ / §三\.\d+ \| \[([^]]+)\]", index)
    assert len(order) == 17
    rework = []
    for name in order:
        stage = ALIASES.get(name, name)
        changes = WIP / ("CHANGES_" + stage + ".md")
        rework.append({"card": name, "changes": changes.name,
                       "status": "selftested" if changes.exists() else "pending",
                       "scope": "offline implementation / evidence preparation only",
                       "independent_acceptance": "pending",
                       "live_evidence_status": "pending" if name in (
                           "WEB2-evidence", "09e-workbuddy-restart-evidence") else "not_run"})
    metadata = {"generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "scope": "isolated WIP; no real UI sends; no final acceptance",
                "original_plan_sha256": digest,
                "historical_text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                "cards": entries, "rework": rework}
    (WIP / "REWORK_STATUS.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ["# Bridge v0.0.9 — isolated WIP 当前状态", "",
            f"生成时间（Asia/Shanghai）：{metadata['generated_at']}。本文件仅在独立WIP维护。", "",
            "原16卡复验按既有报告照录，未重新评判。原结论PASS/FAIL与本轮代码自测分列；所有WIP改动均待不同厂商独立验收。", "",
            "| 原卡 | 原复验结论 | WIP当前处理 | 原始报告副本 |",
            "| --- | --- | --- | --- |"]
    for entry in entries:
        rows.append(f"| {entry['card']} | {entry['recheck_verdict']} | {entry['wip_status']} | [{entry['card']}]({entry['copy']}) |")
    rows += ["", "CARD-05已存在并曾复验通过；插件已有09a/09b正式卡及成品；7b已由CARD-08实现，实际调度/真推不在本任务执行。旧表中的‘无卡/未实现/05未发’仅留作历史。", "",
             "执行顺序以原drafts INDEX为准。17项逐卡变更说明和全量单测见下表；selftested仅指本方离线自测。", "",
             "| 草稿顺序 | 处理 | 变更说明 |", "| --- | --- | --- |"]
    for n, step in enumerate(rework, 1):
        rows.append(f"| {n}. {step['card']} | {step['status']} | [{step['changes']}]({step['changes']}) |")
    rows += ["", "本任务不执行F1-F6/第8项真机流程、不更新正式progress或verdicts、不通知、不发送或重启应用。GLM额度例外仅代表链路到服务端，未冒称token回复。", "",
             "## 历史快照（以下原文不是当前状态表）", "",
             "以下保留原PLAN全文与带日期决定，不删除旧记录；本文件顶部当前表承接后续结果。", "",
             "<!-- ORIGINAL_PLAN_SNAPSHOT_START -->\n" + original + "<!-- ORIGINAL_PLAN_SNAPSHOT_END -->", ""]
    (WIP / "PLAN.md").write_text("\n".join(rows), encoding="utf-8")
    print("WIP PLAN current table + verbatim historical snapshot + 16 report provenance copies rendered.")


if __name__ == "__main__":
    render()
