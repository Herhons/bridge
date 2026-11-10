"""chatgpt 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。

用法：
    python chatgpt_use.py "只回复两个字：收到"
    python chatgpt_use.py "..." --continue
    python chatgpt_use.py --read          ← 只读最后一条回复，不发送

本端要点：
- 输入框锚点是左下角固定的「+」，按偏移 (240,-5) 点进文字行。
- 空输入时右下角是**橙色语音**按钮（波形图标）；输入文字后变成**橙色箭头**。
  颜色一样，只能靠图标形状区分 —— 所以模板必须截"箭头态"。
- 侧栏「新聊天」在左上角；侧栏收起时先点旁边的面板开关（gpt_sidebar）。

CARD-09e 给本端开的三个开关（都在 `Site` 里，别端默认关）：
  · `composer_name="询问 ChatGPT"` + `clear_draft_on_new`：新聊天会**带着上一条
    草稿**，`--new` 之后一读就是 `draft_present`。按 UIA 名字精确认领输入框
    （地址栏也是 EditControl，用名字把两者分开），有草稿就 Ctrl+A / Delete 清掉，
    读回为空才继续。
  · `paste_plain`：行首 `#`/`* `/`1. ` 会被编辑器当 Markdown 渲染掉标记，
    打字版本核对不上时改用粘贴纯文本再写一次。
  · `uia_fallback`：页面答完了却报超时/被打断时，按「最新一条回复 / 回答已完成」
    标记取最后一条回复正文（`qi/lastreply.py`）。
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# 同目录的 choose 脚本要能按名字 import（档位快照复用它的读数实现）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from qi import Library, console, uilock  # noqa: E402
from qi.flow import Site, cli  # noqa: E402

def _blank_new(screen) -> bool:
    """当前页是不是「空的新对话」：页头还挂着 聊天/工作 两个模式词。

    B17：新版界面把**模型**做成按会话隔离的（点一次「新聊天」就回默认 GPT-6
    Luna），而**强度**是账号级的、会留着。于是 `--new` 在 choose 刚开好的新对话
    上再点一次新聊天，就把刚选好的模型抹掉了——档位闸随即报 tier_drift
    （2026-11-09 真机，见 evidence/fail/chatgpt_20261109-212504.png）。
    判据复用 choose 的 `page_kind`：模式词只在新聊天页的页头出现，会话页没有。
    判不出来就返回 False，走原来的「点一次新聊天」，不改别端行为。
    """
    try:
        import chatgpt_choose as choose
    except Exception:                            # noqa: BLE001
        return False
    try:
        return choose.page_kind(screen) == "new"
    except Exception:                            # noqa: BLE001
        return False


SITE = Site(
    name="chatgpt",
    title_hint="Google Chrome",
    composer="gpt_composer",
    send="gpt_send_on",
    actions="gpt_actions",
    copy="gpt_copy",
    composer_offset=(240, -5),
    newchat=("gpt_newchat", "gpt_newchat_icon"),   # 展开=整条，收起=铅笔图标
    sidebar="gpt_sidebar",
    send_mode="exists",
    send_threshold=0.88,
    composer_name="询问 ChatGPT",
    clear_draft_on_new=True,
    paste_plain=True,
    uia_fallback=True,
    blank_new=_blank_new,
)

REQUIRED = ["gpt_composer", "gpt_send_on", "gpt_actions", "gpt_copy"]


# ------------------------------------------------------------ B17 档位快照
# 新版 ChatGPT 界面把档位文本从芯片上撤掉了：Button 只剩「选择 ChatGPT 模型」
# 一个名字，没有子 TextControl、没有 Value/HelpText，整页再找不到**只读**的
# 档位文本（2026-11-09 实测，见 evidence/batch17/chatgpt_snapshot_probe.txt）。
# 唯一能读到真值的路径是「开弹层 → 读滑杆可访问值 / 读 RadioButton 选中项 →
# Esc 收起」，而 `sendgate.pre_send` 是在**文字已经写进输入框之后**才跑：
# 那时再点芯片、按 Esc，会把 guard 的 last_input 推开，紧接着的 submit 就会
# 被判成 user_took_over。所以读数挪到写入之前（新对话开好、草稿清掉之后），
# 值通过 `reread` 交给档位闸复核。
#
# 新鲜度：快照与发送之间只隔一次 write（打字+粘贴，约 1~3s），但抢 UI 段锁
# 最多要等 uilock.SECTION_TIMEOUT=60s，所以上限放到一次发送预算。这段时间里
# 若真有人动键鼠，guard 本来就会判 user_took_over——不需要靠"短龄"兜底。
SNAPSHOT_MAX_AGE = 120.0
_SNAPSHOT: dict = {}


def take_snapshot(screen) -> dict:
    """读一次 writer/model/effort 存进 `_SNAPSHOT`。

    **不抛异常**：读不到也记下来，由档位闸按 fail-closed 判死
    （回执不成立 → stage=tier_unconfirmed / 退出码 5），绝不用回执里的目标值顶替。
    """
    _SNAPSHOT.clear()
    started = time.monotonic()
    try:
        import chatgpt_choose as choose          # 同目录脚本：复用同一套读数实现
    except Exception as exc:                     # noqa: BLE001 - 导入失败也只算读数失败
        _SNAPSHOT.update({"error": f"import: {type(exc).__name__}: {exc}", "at": started})
        return dict(_SNAPSHOT)
    try:
        writer = (choose.read_writer(screen) or {}).get("writer")
        if writer not in choose.EFFORTS:
            _SNAPSHOT.update({"error": "writer_unknown", "writer": writer, "at": started})
            return dict(_SNAPSHOT)
        model = choose.read_model(screen, writer) or {}
        effort = choose.read_effort(screen, writer) or {}
        _SNAPSHOT.update({
            "writer": writer,
            "model": model.get("model"),
            "effort": effort.get("effort"),
            "model_source": model.get("source"),
            "effort_source": effort.get("source"),
            "at": time.monotonic(),
            "elapsed": round(time.monotonic() - started, 1),
        })
    except Exception as exc:                     # noqa: BLE001 - 同上
        _SNAPSHOT.update({"error": f"{type(exc).__name__}: {exc}", "at": started})
    return dict(_SNAPSHOT)


def snapshot_read(dimension: str = "composite") -> dict:
    """给档位闸当 `reread`：只交刚才那次快照，**不在这里动任何 UI**。"""
    snap = dict(_SNAPSHOT)
    if not snap:
        return {"value": None, "source": "use_snapshot", "reason": "snapshot_missing"}
    if snap.get("error"):
        return {"value": None, "source": "use_snapshot",
                "reason": f"snapshot_failed: {snap['error']}"}
    age = time.monotonic() - float(snap.get("at") or 0.0)
    stamp = f"age={age:.1f}s"
    dims = {"model": snap.get("model"), "effort": snap.get("effort")}
    if dimension != "composite":
        return {"value": dims.get(dimension), "source": "use_snapshot", "raw": stamp,
                "writer": snap.get("writer"), "dimensions": dims}
    if age > SNAPSHOT_MAX_AGE:
        return {"value": None, "source": "use_snapshot", "raw": stamp,
                "reason": f"snapshot_stale: {age:.1f}s > {SNAPSHOT_MAX_AGE:.0f}s"}
    if not snap.get("writer") or not all(dims.values()):
        missing = [k for k, v in dims.items() if not v]
        return {"value": None, "source": "use_snapshot", "raw": stamp,
                "reason": "snapshot_incomplete", "missing": missing, "values": dims}
    return {"writer": snap["writer"], "dimensions": dims, "value": dims,
            "source": "use_snapshot",
            "raw": f"{stamp} model={dims['model']} effort={dims['effort']}"}


def prepare(screen) -> dict:
    """`flow.ask` 在「新对话开好、草稿清掉、还没写文字」时调这个。

    这十几秒要开菜单、点条目、按 Esc——和 `*_choose` 一样是"动桌面"的活，
    必须放进短临界区：并发模式下别的席位随时能把前台抢走，那时再按坐标点下去
    就点到别人页面上了（`uilock.section` 同线程可重入，和整轮锁不冲突）。
    """
    with uilock.section("tier:chatgpt"):
        try:
            screen.focus()
        except Exception:                        # noqa: BLE001 - 聚焦失败只影响读数
            pass
        snap = take_snapshot(screen)
    return {"ok": True, "snapshot": {k: v for k, v in snap.items() if k != "at"}}


def main() -> int:
    # --read 只读、不发送：不需要输入框/发送键这些模板
    if "--read" in sys.argv[1:]:
        return cli(SITE, sys.argv[1:])
    lib = Library()
    need = list(REQUIRED)
    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):
        need.append("gpt_newchat")
    print(lib.report(need))
    if lib.missing(need):
        return 3
    return cli(SITE, sys.argv[1:], prepare=prepare, reread=snapshot_read)


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.entry("chatgpt_use", main))
