"""判断 Chrome 有没有在放视频/音频。

为什么需要它：脚本要复用一个已经开着的 Chrome 时，必须知道用户是不是正在用它。
"有没有在播音频"是最可靠的信号——放视频几乎一定出声，而静态网页不会。
用窗口标题、前台状态都判断不出来（用户可能把播放器放后台）。

实现走 Windows 音频会话（WASAPI）：Chrome 的渲染进程一出声就会有一个
AudioSessionStateActive 的会话。依赖 pycaw。

2026-11-01 CARD-WEB：缺 pycaw 时原来**静默**返回 True，结果每轮都"新开窗口"，
桌面越堆越多，日志里还只写「Chrome 在播音频: True」，看不出是检测不可用。
现在拆成两层：
  · `probe()` 说清楚"能不能测、测到的是什么"，缺库时带原因（hint）；
  · `chrome_playing_audio()` 保持原来的保守语义（测不了就当在播放），
    调用方要提示就调 probe()，别自己猜。
"""

from __future__ import annotations

CHROME_EXE = "chrome.exe"
_AUDIO_ACTIVE = 1          # AudioSessionStateActive

MISSING_HINT = ("音频检测不可用：没有安装 pycaw（装法 pip install pycaw），"
                "只能按「正在播放」处理，会新开窗口")


def probe() -> dict:
    """测一次，把"能不能测"和"测到什么"分开报。

    返回 {"available": bool, "playing": bool | None, "hint": str}
      · available=False → playing=None，hint 说明为什么测不了；
      · available=True  → playing 是真实结果，hint 为空。
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:                      # noqa: BLE001 - 缺库/装坏了都算不可用
        return {"available": False, "playing": None,
                "hint": f"{MISSING_HINT}；原因：{type(exc).__name__}: {exc}"}

    try:
        for sess in AudioUtilities.GetAllSessions():
            proc = sess.Process
            if proc is None:
                continue
            if proc.name().lower() != CHROME_EXE:
                continue
            if int(sess.State) == _AUDIO_ACTIVE:
                return {"available": True, "playing": True, "hint": ""}
    except Exception as exc:                      # noqa: BLE001 - 查询失败也走保守分支
        return {"available": False, "playing": None,
                "hint": f"音频检测不可用：查询音频会话失败（{type(exc).__name__}: {exc}）"}
    return {"available": True, "playing": False, "hint": ""}


def chrome_playing_audio() -> bool:
    """Chrome 进程是否有活跃音频会话。

    返回 True 也可能是"在放音乐"而不是视频，但两种情况都不该去动它。
    测不了的时候返回 True（宁可多开一个窗口，也不去打扰用户可能正在看的东西）——
    调用方想知道"这是真在播还是测不了"，看 probe()。
    """
    info = probe()
    if info["available"]:
        return bool(info["playing"])
    return True


def describe() -> list[dict]:
    """调试用：列出所有音频会话。"""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return []
    out = []
    for sess in AudioUtilities.GetAllSessions():
        proc = sess.Process
        out.append({
            "name": proc.name() if proc else "(system)",
            "pid": proc.pid if proc else 0,
            "state": int(sess.State),
        })
    return out
