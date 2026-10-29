"""判断 Chrome 有没有在放视频/音频。

为什么需要它：脚本要复用一个已经开着的 Chrome 时，必须知道用户是不是正在用它。
"有没有在播音频"是最可靠的信号——放视频几乎一定出声，而静态网页不会。
用窗口标题、前台状态都判断不出来（用户可能把播放器放后台）。

实现走 Windows 音频会话（WASAPI）：Chrome 的渲染进程一出声就会有一个
AudioSessionStateActive 的会话。依赖 pycaw；没装时**返回 True（保守）**，
让调用方走"新开窗口"这条路——多开一个窗口只是多余，打扰到用户才是真问题。
"""

from __future__ import annotations

CHROME_EXE = "chrome.exe"
_AUDIO_ACTIVE = 1          # AudioSessionStateActive


def chrome_playing_audio() -> bool:
    """Chrome 进程是否有活跃音频会话。

    返回 True 也可能是"在放音乐"而不是视频，但两种情况都不该去动它。
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return True        # 没装检测库：按"正在用"处理，宁可多开一个窗口

    try:
        for sess in AudioUtilities.GetAllSessions():
            proc = sess.Process
            if proc is None:
                continue
            if proc.name().lower() != CHROME_EXE:
                continue
            if sess.State == _AUDIO_ACTIVE:
                return True
    except Exception:
        return True        # 查询本身出错也走保守分支
    return False


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
