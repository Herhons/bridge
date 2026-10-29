"""一次性补丁：gemini_choose 的三个分支也改绑句柄。用完即删。"""

import pathlib

p = pathlib.Path("src/scripts/gemini_choose.py")
t = p.read_text(encoding="utf-8")

t = t.replace(
    "from qi import Library, Screen  # noqa: E402",
    "from qi import Library, Screen  # noqa: E402\nfrom qi.browser import ensure_window  # noqa: E402",
)

old = '''        sc = Screen()
        if not sc.bring_to_front(CFG.title_hint, on_secondary=CFG.on_secondary):
            print("找不到浏览器窗口"); return 1'''

new = '''        sc = Screen()
        hwnd = ensure_window()
        if hwnd is None:
            print("开不出浏览器窗口"); return 1
        sc.bind(hwnd)
        if not sc.focus():
            print("窗口提不到前台"); return 1
        time.sleep(1.2)'''

n = t.count(old)
t = t.replace(old, new)
p.write_text(t, encoding="utf-8")
print("替换处数:", n)
