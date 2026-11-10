"""UI-BOOTSTRAP-3 / opencode **真实入口**：从 `opencode_use.main()` 走到发送前门禁。

为什么不用"直接调 `_reread_tier`"代替：04:01:10 那次失败是
    `tier_unconfirmed / tier_unreadable / dimension=model / source=none`
—— 它是 `use.ask()` → `qa.conversation.ask(pre_send=door)` → `tier_guard.gate`
这条链上冒出来的。只测单个函数等于测不到"入口有没有用对识别器"。

这里把窗口换成**真实模板拼的合成图**（居中 composer，实测坐标），
把 `qi.conversation.ask` 换成会**真的调用 pre_send()** 的桩，
其余（`use.ask`、`_reread_tier`、`read_model`、`tier_guard.gate`、回执读写）全是真的。

不启窗口、不点击、不发送、不写 runs/（runlog/failure 都打桩到内存）。
"""
from offline_guard import ensure_offline
ensure_offline()

import importlib.util
import json
import os
import sys
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "src" / "scripts", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from qi import tier_guard as tg  # noqa: E402

LIBRARY = ROOT / "src" / "assets" / "library"
WIN = (0, 0, 1296, 808)
FAKE_HWND = 987654321          # IsWindow 一定返回 0：任何真 UI 调用都不会落到别人窗口上
PROMPT = "只回复两个字母：OK"


def load_use():
    """按路径装模块：它不在包里，也不自己 import 项目代码。"""
    path = ROOT / "src" / "scripts" / "opencode_use.py"
    spec = importlib.util.spec_from_file_location("r3_opencode_use", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


USE = load_use()


def tpl(name):
    img = cv2.imread(str(LIBRARY / f"{name}.png"), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(name)
    return img


def paste(canvas, name, x, y):
    img = tpl(name)
    h, w = img.shape[:2]
    canvas[y:y + h, x:x + w] = img


def blank_canvas():
    """首页/会话列表：一整页白，没有发送键、没有输入框占位文字。"""
    return np.full((WIN[3], WIN[2], 3), 255, dtype=np.uint8)


def centered_canvas(*, model="oc_m_glm53free", chevron_x=476, send_x=1051, top=387):
    """空白新会话：composer 在页面中央（2026-11-07 实测坐标）。"""
    canvas = blank_canvas()
    paste(canvas, "oc_placeholder", 225, top)
    paste(canvas, model, 339, top + 40)
    paste(canvas, "oc_chevron", chevron_x, top + 45)
    paste(canvas, "oc_send", send_x, top + 34)
    return canvas


class RecordingDesk(USE.Desk):
    """真的 Desk（真模板匹配），抓图换成内存合成图，并记下每次搜索的区域。"""

    def __init__(self, canvas, hwnd=FAKE_HWND):
        super().__init__(hwnd)
        self.canvas = canvas
        self.clicks = []
        self.keys = []
        self.finds = []
        self.find_alls = []

    def grab(self, region=None, *, fresh=False):
        if region is None:
            return self.canvas.copy()
        x, y, w, h = (int(v) for v in region)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.canvas.shape[1], x + w), min(self.canvas.shape[0], y + h)
        if x1 <= x0 or y1 <= y0:
            return np.zeros((0, 0, 3), np.uint8)
        return self.canvas[y0:y1, x0:x1].copy()

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))

    def press(self, key):
        self.keys.append(key)

    def hotkey(self, *keys):
        self.keys.append("+".join(keys))

    def type_text(self, text, interval=0.004):
        self.keys.append("type:" + text)

    def find(self, name, threshold=USE.TPL_THR, region=None):
        self.finds.append((name, None if region is None else tuple(region)))
        return super().find(name, threshold, region)

    def find_all(self, name, threshold=0.85, region=None, nms=6):
        self.find_alls.append((name, None if region is None else tuple(region)))
        return super().find_all(name, threshold, region, nms)


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top = left, top
        self.right, self.bottom = right, bottom


class FakeControl:
    def __init__(self, rect):
        self.BoundingRectangle = rect


class FakeNode:
    def __init__(self, kind, name, rect=None):
        self.kind, self.name = kind, name
        self.control = FakeControl(rect or FakeRect(0, 0, 0, 0))
        self.children = []

    def walk(self):
        return [self]


class FakeRoot:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    def walk(self):
        return iter(self.nodes)


def uia_root(chip=None, composer=True):
    nodes = [FakeNode("ButtonControl", "新建会话", FakeRect(1064, 8, 1092, 36))]
    if composer:
        nodes.append(FakeNode("EditControl", "提示词", FakeRect(208, 345, 1080, 550)))
    if chip:
        nodes.append(FakeNode("ButtonControl", chip, FakeRect(248, 557, 496, 586)))
    return FakeRoot(nodes)


class FakeConversation:
    """替身只做一件事：**真的**把 pre_send 门跑一遍，其余按 conversation.ask 的契约回。"""

    def __init__(self, sink):
        self.sink = sink

    def __call__(self, conversation, prompt, timeout, pre_send=None, **kwargs):
        # T2-1 接线之后，真实入口会多传 `purpose=`；替身按 `conversation.ask` 的
        # 契约一并用 **kwargs 收下，避免签名一变就 TypeError。
        self.sink["asks"].append({"prompt": prompt, "timeout": timeout,
                                  "purpose": kwargs.get("purpose")})
        if pre_send is not None:
            try:
                door = pre_send() or {}
            except tg.TierUnconfirmed as exc:
                self.sink["refused"] = (exc.reason, exc.detail)
                return {**exc.as_result(), "submission": "not_sent", "sent": False}
            self.sink["stamp"] = door.get("stamp")
        self.sink["submitted"] = True
        return {"ok": True, "stage": "answered", "submission": "accepted",
                "sent": True, "reply": "OK", "elapsed": 0.1}


class Harness:
    """把 main() 外面的世界换成假的：窗口、前台、Desk、UIA、日志、conversation。"""

    def __init__(self, desk, *, root=None, chip=None, composer=True, uia=True):
        self.desk = desk
        self.root = root or tempfile.mkdtemp(prefix="r3_use_")
        self.logs = []
        self.sink = {"asks": [], "submitted": False, "refused": None, "stamp": None}
        self.chip = chip
        self.composer = composer
        self.uia = uia

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        stack = self.stack
        self.out = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(mock.patch.object(tg, "ROOT", Path(self.root)))
        stack.enter_context(mock.patch.object(USE, "find_window", return_value=FAKE_HWND))
        stack.enter_context(mock.patch.object(USE, "rect_of", return_value=WIN))
        stack.enter_context(mock.patch.object(USE, "activate", return_value=True))
        stack.enter_context(mock.patch.object(USE, "Desk", return_value=self.desk))
        stack.enter_context(mock.patch("time.sleep"))
        stack.enter_context(mock.patch.object(
            USE, "_uia_root",
            return_value=None if not self.uia else uia_root(self.chip, self.composer)))
        stack.enter_context(mock.patch(
            "qi.conversation.ask", side_effect=FakeConversation(self.sink)))
        stack.enter_context(mock.patch(
            "qi.runlog.record", side_effect=lambda endpoint, result, **kw: self.logs.append(
                dict(result, endpoint=endpoint))))
        stack.enter_context(mock.patch("qi.failure.record", return_value={}))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def write_receipt(root, *, target="glm_53_flash_free", readback=None, dimension="model"):
    return tg.write_receipt("opencode", target=target,
                            readback=readback if readback is not None else target,
                            dimension=dimension, root=root, now=tg.utc_now(),
                            hwnd=FAKE_HWND)


class PreSendRereadOnRealEntry(unittest.TestCase):
    """main() -> ask() -> door() -> gate()：读回必须走**居中 composer + UIA 身份**。"""

    def test_uia_identity_confirms_the_model_before_sending(self):
        desk = RecordingDesk(centered_canvas())
        with tempfile.TemporaryDirectory() as tmp, \
                Harness(desk, root=tmp, chip="OpenCode / GLM-5.3-Flash Free") as h:
            write_receipt(tmp)
            code = USE.main([PROMPT, "--purpose", "accept_lite", "--force"])
            left = tg.read_receipt("opencode", root=tmp)

        self.assertEqual(code, 0, h.logs)
        self.assertEqual(len(h.sink["asks"]), 1, "应当走到发送那一步")
        self.assertTrue(h.sink["submitted"])
        self.assertIsNone(h.sink["refused"])
        self.assertEqual(h.logs[-1]["purpose_label"], "PASS(GLM-lite)")
        # 回执被发送前的门消费掉了（成功路径的唯一消费点）
        self.assertTrue(left and left["consumed"], "成功发送后回执必须已被消费")

    def test_centered_composer_is_used_when_uia_is_unavailable(self):
        """UIA 读不到时退回模板，但必须在**居中**那一行里找，不是距底 170px。"""
        desk = RecordingDesk(centered_canvas())
        with tempfile.TemporaryDirectory() as tmp, \
                Harness(desk, root=tmp, uia=False) as h:
            write_receipt(tmp)
            code = USE.main([PROMPT, "--purpose", "accept_lite", "--force"])

        self.assertEqual(code, 0, h.logs)
        self.assertTrue(h.sink["submitted"])
        # 发送键是在整窗里找的（旧实现只在 (0,638,1296,170) 里找）
        self.assertIn(("oc_send", tuple(WIN)), desk.finds)
        self.assertFalse([r for name, r in desk.finds
                          if name == "oc_send" and r and r[1] >= 600],
                         "不许再把搜索区锁在底部带")
        # ⌄ 的搜索带中心必须在 436 附近（居中 composer 那一行）
        bands = [r for name, r in desk.find_alls if name == "oc_chevron" and r]
        self.assertTrue(bands, desk.find_alls)
        tops = sorted({r[1] for r in bands})
        self.assertTrue(any(400 <= top <= 440 for top in tops), tops)
        self.assertFalse([top for top in tops if top >= 600], tops)

    def test_model_text_far_from_its_chevron_is_not_confirmed(self):
        """模板命中但旁边没有它自己的 ⌄：控制身份不成立 -> 拒发（不是猜一个模型）。"""
        canvas = blank_canvas()
        paste(canvas, "oc_placeholder", 225, 387)
        paste(canvas, "oc_m_glm53free", 339, 427)
        paste(canvas, "oc_chevron", 900, 432)          # 离模型文字 430px
        paste(canvas, "oc_send", 1051, 421)
        desk = RecordingDesk(canvas)
        with tempfile.TemporaryDirectory() as tmp, \
                Harness(desk, root=tmp, uia=False) as h:
            write_receipt(tmp)
            code = USE.main([PROMPT, "--purpose", "accept_lite", "--force"])
            left = tg.read_receipt("opencode", root=tmp)

        self.assertEqual(code, 5, h.logs)
        self.assertFalse(h.sink["submitted"], "没确认就不许发送")
        self.assertEqual(h.sink["refused"][0], "tier_unreadable")
        self.assertTrue(left and left["consumed"],
                        "失败也要作废回执（CARD-06a 语义）")

    def test_unknown_uia_chip_refuses_and_reports_the_source(self):
        desk = RecordingDesk(centered_canvas())
        with tempfile.TemporaryDirectory() as tmp, \
                Harness(desk, root=tmp, chip="OpenCode / Some Other Model") as h:
            write_receipt(tmp)
            code = USE.main([PROMPT, "--purpose", "accept_lite", "--force"])

        self.assertEqual(code, 5, h.logs)
        self.assertFalse(h.sink["submitted"])
        reason, detail = h.sink["refused"]
        self.assertEqual(reason, "tier_unreadable")
        self.assertEqual(detail["dimension"], "model")
        self.assertEqual(detail["source"], "uia", "失败证据里要看得见'靠 UIA 读过'")

    def test_drift_is_refused_without_reselecting(self):
        """回执写的是另一个模型：读到不一致 -> tier_drift，不点菜单、不重选。"""
        desk = RecordingDesk(centered_canvas())
        with tempfile.TemporaryDirectory() as tmp, \
                Harness(desk, root=tmp, chip="OpenCode / GLM-5.3-Flash Free") as h:
            write_receipt(tmp, target="deepseek_v41_flash_free")
            code = USE.main([PROMPT, "--purpose", "test", "--force"])

        self.assertEqual(code, 5, h.logs)
        self.assertFalse(h.sink["submitted"])
        self.assertEqual(h.sink["refused"][0], "tier_drift")
        self.assertEqual(desk.clicks, [], "use 绝不自己切档")

    def test_glm_keeps_no_effort_semantics(self):
        """GLM 只有一个 ⌄（没有 variant）：effort=none，且复核不会编出 default。"""
        desk = RecordingDesk(centered_canvas())
        self.assertEqual(USE.read_effort(desk, WIN)["effort"], "none")
        self.assertEqual(USE._reread_tier(desk, WIN, "effort")["value"], None,
                         "读不到 effort 就是 None（拒发），不能冒充 default")


class HomeScreen(unittest.TestCase):
    """首页/会话列表：没有 composer。**一个字都不许打**。"""

    def test_prompt_is_refused_without_typing(self):
        desk = RecordingDesk(blank_canvas())
        with tempfile.TemporaryDirectory() as tmp, Harness(desk, root=tmp, uia=False) as h:
            write_receipt(tmp)
            code = USE.main([PROMPT, "--purpose", "test", "--force"])
            # 回执没被消费：这次连门都没走到（可以清完草稿后重试）
            kept = tg.read_receipt("opencode", root=tmp)

        self.assertEqual(code, 1, h.logs)
        self.assertEqual(h.sink["asks"], [], "conversation 一步都不该进")
        self.assertEqual(desk.keys, [], "没写字、没按键、没粘贴")
        self.assertEqual(desk.clicks, [], "也没点过任何东西")
        self.assertEqual(h.logs[-1]["stage"], "session_not_open")
        self.assertFalse((kept or {}).get("consumed"),
                         "门都没走到，回执不该被作废")

    def test_list_on_home_fails_closed(self):
        desk = RecordingDesk(blank_canvas())
        with tempfile.TemporaryDirectory() as tmp, Harness(desk, root=tmp, uia=False) as h:
            code = USE.main(["--list", "--force"])

        self.assertEqual(code, 1, "读不到模型要退 1，不能把'没读到'记成通过")
        self.assertEqual(h.logs[-1]["stage"], "session_not_open")

    def test_new_opens_a_session_and_waits_for_the_composer(self):
        desk = RecordingDesk(blank_canvas())
        calls = []

        def new_chat(_screen, site):
            calls.append(site)
            desk.canvas = centered_canvas()      # 应用真的开出了新会话

        with tempfile.TemporaryDirectory() as tmp, Harness(desk, root=tmp, uia=False) as h, \
                mock.patch("qi.workbuddy.new_chat", side_effect=new_chat):
            code = USE.main(["--new", "--force"])

        self.assertEqual(code, 0, h.logs)
        self.assertEqual(calls, ["opencode"], "必须用应用自己的'新建会话'")
        self.assertEqual(h.logs[-1]["stage"], "new_session")
        self.assertEqual(h.logs[-1]["session"]["state"], "session")
        self.assertEqual(desk.keys, [], "等 composer 的过程里不许打字")

    def test_new_that_never_shows_a_composer_is_refused(self):
        desk = RecordingDesk(blank_canvas())
        real_wait = USE.wait_composer
        with tempfile.TemporaryDirectory() as tmp, Harness(desk, root=tmp, uia=False) as h, \
                mock.patch("qi.workbuddy.new_chat") as new_chat, \
                mock.patch.object(USE, "wait_composer",
                                  side_effect=lambda d, win: real_wait(d, win, 0.0)):
            code = USE.main([PROMPT, "--purpose", "test", "--new", "--force"])

        self.assertEqual(code, 1, h.logs)
        new_chat.assert_called_once()
        self.assertEqual(h.sink["asks"], [])
        self.assertEqual(h.logs[-1]["stage"], "session_not_open")


class UiaGuard(unittest.TestCase):
    def test_bogus_handle_never_reaches_uia(self):
        """句柄不是真窗口就不碰 UIA（离线护栏也不该被惊动）。"""
        self.assertIsNone(USE._uia_root(SimpleNamespace(hwnd=FAKE_HWND)))
        self.assertIsNone(USE._uia_root(SimpleNamespace(hwnd=0)))

    def test_session_state_prefers_uia_composer_over_the_picture(self):
        """UIA 有 composer 就直接算会话，不再依赖截图（省一次抓图，也更硬）。"""
        desk = RecordingDesk(blank_canvas())
        with mock.patch.object(USE, "_uia_root", return_value=uia_root(chip="x")):
            got = USE.session_state(desk, WIN)
        self.assertEqual(got["state"], "session")
        self.assertEqual(got["source"], "uia")

    def test_search_box_of_home_is_too_short_to_be_a_composer(self):
        """首页的'搜索会话'框宽 600 高 30：尺寸判据要把它排掉。"""
        root = FakeRoot([FakeNode("EditControl", "在 Bridge 中搜索会话",
                                  FakeRect(370, 78, 970, 108))])
        with mock.patch.object(USE, "_uia_root", return_value=root):
            self.assertIsNone(USE.composer_edit(RecordingDesk(blank_canvas())))


if __name__ == "__main__":
    unittest.main()
