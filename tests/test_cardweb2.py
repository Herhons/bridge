"""CARD-WEB2：网页端会审的三处真机 bug 的离线钉子。

钉四件事（对应卡面「做什么」1~4）：

1. **UIA 优先 / 模板回落的选择逻辑。**
   Gemini 的「发起新对话」在控件树里是 `H:` + `T:`，**没有一个是 Button**；
   Grok 的发送键在输入文字后叫「提交」。这两条只要退回纯模板就是死路
   （`grok_send_on` 现场最高 0.490，`gem_send_on` 0.652，阈值 0.95）。
2. **模板真的失配时，报错里要能看见分数和所在屏幕/缩放。**
   10:22 那句「最高 0.490 / 阈值 0.95」看不出是缩放问题还是找错了屏。
3. **超时前最后一次读取。** 10:35 那轮回复早就写完了（`--read` 一读就有），
   等待循环却因为认领不到提问白等到 300s 报 `no_reply`。
4. **`grok_use --read` 的参数契约与输出格式**，与 `gemini_use --read` 一致。

全部离线：不开窗口、不截图、不点击真实屏幕。
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import gemini, ui  # noqa: E402
from qi.screen import Match  # noqa: E402

KINDS = {"B": "ButtonControl", "T": "TextControl", "H": "HyperlinkControl",
         "L": "ListItemControl", "E": "EditControl", "G": "GroupControl"}


# ---------------------------------------------------------------- 打桩工具
class Ctrl:
    """控件替身：只要够 ui.click / ui.named 用就行。"""

    IsEnabled = True
    IsOffscreen = False
    BoundingRectangle = SimpleNamespace(left=0, top=0, right=20, bottom=20)


def flat(*lines: str) -> ui.Node:
    """把 "B:xxx" / "T:xxx" 这样平铺的行搭成一棵树（文档序＝行序）。"""
    root = ui.Node("DocumentControl", "")
    for line in lines:
        if len(line) < 2 or line[1] != ":":
            continue
        kind, name = line[0], line[2:]
        if name:
            root.children.append(ui.Node(KINDS.get(kind, "TextControl"), name, Ctrl(), root))
    return root


class StubScreen:
    """只回答「在哪块屏 / 模板分数 / 命中框」，不截图。"""

    target_hwnd = 401149816

    def __init__(self, matches=None, best=None, where="显示器 -1440,-653 1440x2560；缩放 125%"):
        self.matches = matches or {}
        self.last_best = best
        self.where = where
        self.find_calls = []
        self.clicked = []

    def window_rect(self, *args, **kwargs):
        return (-1448, -661, 1456, 2536)

    def find(self, name, *, threshold=None, region=None, scales=(1.0,)):
        self.find_calls.append((name, tuple(scales)))
        return self.matches.get(name)

    def click(self, match, *, offset=(0, 0)):
        self.clicked.append(match.center)

    def click_xy(self, x, y):
        self.clicked.append((x, y))

    def describe(self, region=None):
        return self.where


class Clock:
    """把等待变成瞬移：sleep 多少，monotonic 就走多少。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def load_use(filename: str):
    """按路径装 src\\scripts 下的脚本（它们不在包里，但会 import 项目代码）。"""
    name = "cardweb2_" + Path(filename).stem
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 1. 新对话
class GeminiNewChat(unittest.TestCase):
    """「发起新对话」不是 Button，而且页面刚打开时读一次树读不到。"""

    def run_open(self, trees, screen):
        control = object()
        with mock.patch.object(gemini.ui, "document", return_value=(control, "https://gemini.google.com/app")), \
                mock.patch.object(gemini.ui, "tree", side_effect=list(trees)), \
                mock.patch.object(gemini.ui, "click") as clicked, \
                mock.patch.object(gemini.time, "sleep"):
            result = gemini.open_new_chat(screen, SimpleNamespace(name="gemini", newchat="gem_newchat",
                                                                  title_hint="Google Chrome",
                                                                  on_secondary=True))
        return result, clicked

    def test_named_link_wins_and_no_template_is_used(self):
        screen = StubScreen()
        result, clicked = self.run_open([flat("H:发起新对话", "T:发起新对话", "T:问问 Gemini")], screen)

        self.assertTrue(result["ok"])
        self.assertEqual(result["basis"], "uia")
        self.assertEqual(result["role"], "HyperlinkControl", "同一名字挂两种角色时先点链接")
        self.assertEqual(screen.find_calls, [], "UIA 找到时不该再去截图")
        clicked.assert_called_once()

    def test_sidebar_that_is_not_rendered_yet_is_retried(self):
        """10:20 那轮：新窗口刚打开，第一次读到的树里还没有侧栏。"""
        screen = StubScreen()
        trees = [flat("T:与 Gemini 对话"), flat("T:与 Gemini 对话"), flat("H:发起新对话")]

        result, clicked = self.run_open(trees, screen)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reads"], 3)
        self.assertEqual(result["basis"], "uia")
        clicked.assert_called_once()

    def test_template_is_the_fallback(self):
        screen = StubScreen(matches={"gem_newchat": Match("gem_newchat", 0.914, 100, 200, 10, 10)})
        result, _ = self.run_open([flat("T:与 Gemini 对话")] * 8, screen)

        self.assertTrue(result["ok"])
        self.assertEqual(result["basis"], "template")
        self.assertEqual(screen.clicked, [(105, 205)])
        self.assertEqual(screen.find_calls[0][1], (1.0, 0.9, 0.8, 1.1, 1.25),
                         "页面缩放会变，模板要按几个常见缩放各匹一次")

    def test_missing_everywhere_reports_score_screen_and_scale(self):
        screen = StubScreen(best=0.4217)
        with mock.patch.object(gemini.ui, "page_zoom", return_value="125%"):
            result, _ = self.run_open([flat("T:与 Gemini 对话")] * 8, screen)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "newchat_not_found")
        self.assertIn("0.422", result["hint"])
        self.assertIn("缩放 125%", result["hint"])
        self.assertIn("-1440,-653", result["hint"])
        self.assertIn("页面缩放 125%", result["hint"])


# ---------------------------------------------------------------- 2. grok 控件树
class GrokTree(unittest.TestCase):
    """Grok 的树：标签只出现一次，历史列表顶上那个同名「聊天」不是新对话。"""

    def setUp(self):
        self.use = load_use("grok_use.py")

    def test_turn_is_split_into_prompt_and_reply(self):
        root = flat("T:你", "T:只回复 OK-WEB2", "B:Edit", "B:复制",
                    "T:Grok", "T:OK-WEB2", "B:复制响应", "B:Like")
        scan = self.use.scan(root)

        self.assertEqual([b.label for b in scan.blocks], ["prompt", "reply"])
        self.assertEqual(scan.blocks[1].text, "OK-WEB2")
        self.assertTrue(scan.done(scan.blocks[1]))
        self.assertFalse(scan.done(scan.blocks[0]), "提问条上的「复制」不能算回答收尾")

    def test_new_chat_prefers_the_link_over_the_history_header(self):
        """真机树里历史列表顶上也有一个 ButtonControl 叫「聊天」。"""
        root = flat("H:聊天", "T:聊天", "B:聊天", "H:只回复 OK-WEB2-PROBE")
        nodes = self.use.new_chat_button(root)
        self.assertTrue(nodes)
        self.assertEqual(nodes[0].kind, "HyperlinkControl")

    def test_collapsed_prompt_still_matches(self):
        prompt = "只回复这一行：OK-WEB2 请保持原样" * 20
        self.assertTrue(self.use.prompt_matches(prompt[:40] + "…", prompt))
        self.assertFalse(self.use.prompt_matches("完全无关的一段话" * 3, prompt))

    def test_reply_without_bar_is_not_done(self):
        root = flat("T:你", "T:提问内容足够长足够长足够长", "T:Grok", "T:写了一半")
        scan = self.use.scan(root)
        reply = scan.reply_after(scan.prompt_at("提问内容足够长足够长足够长"))
        self.assertIsNotNone(reply)
        self.assertFalse(scan.done(reply))


# ---------------------------------------------------------------- 3. 发送键
class Submit(unittest.TestCase):
    """发送：UIA 优先（名字「提交/发送」），模板只作兜底且报错要带分数。"""

    def setUp(self):
        self.use = load_use("grok_use.py")

    def site(self):
        return SimpleNamespace(name="grok", title_hint="Google Chrome", on_secondary=True,
                               send="grok_send_on", send_threshold=0.90)

    def view(self, *lines):
        return SimpleNamespace(root=flat(*lines))

    def test_named_submit_button_wins(self):
        screen = StubScreen()
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        with mock.patch.object(self.use.guard, "user_moved_since", return_value=False):
            how = self.use._submit(screen, self.site(), conv, self.view("B:提交", "B:附件"))
        self.assertEqual(how, "uia")
        self.assertEqual(screen.find_calls, [], "有真按钮时不该去截图找模板")

    def test_template_is_the_fallback(self):
        screen = StubScreen(matches={"grok_send_on": Match("grok_send_on", 0.93, 100, 200, 10, 10)})
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        with mock.patch.object(self.use.guard, "user_moved_since", return_value=False), \
                mock.patch.object(self.use.time, "sleep"):
            how = self.use._submit(screen, self.site(), conv, self.view("T:只有文字没有按钮"))

        self.assertEqual(how, "template")
        self.assertEqual(screen.clicked, [(105, 205)])
        self.assertEqual(screen.find_calls[0][1], (1.0, 0.9, 0.8, 1.1, 1.25))

    def test_no_send_key_at_all_names_the_score_and_scale(self):
        screen = StubScreen(best=0.490)
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        with mock.patch.object(self.use.guard, "user_moved_since", return_value=False), \
                mock.patch.object(self.use.ui, "page_zoom", return_value="125%"), \
                mock.patch.object(self.use.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self.use._submit(screen, self.site(), conv, self.view("T:什么都没有"))

        message = str(ctx.exception)
        self.assertIn("0.490", message)
        self.assertIn("缩放 125%", message)
        self.assertIn("页面缩放 125%", message, "模板是在页面缩放下截的，报错要带这个")


# ---------------------------------------------------------------- 4. 完成检测
def view(root, value="", busy=False, url="https://gemini.google.com/app/abc"):
    return SimpleNamespace(root=root, value=value, busy=busy, url=url,
                           editor=None, copies=[], site="gemini")


class FakeConversation:
    """按顺序吐视图：before → written → 每次轮询 → 最后那次重读。"""

    def __init__(self, views):
        self.views = list(views)
        self.i = 0
        self.writes = []

    def read(self, attempts=4, delay=0.4):
        index = min(self.i, len(self.views) - 1)
        self.i += 1
        return self.views[index]

    def write(self, view_, prompt):
        self.writes.append(prompt)

    def copy(self, node):
        return ""


class GeminiWait(unittest.TestCase):
    """认领不到提问时不能白等到超时；回复写完了就得交卷。"""

    SITE = SimpleNamespace(name="gemini", title_hint="Google Chrome", on_secondary=True,
                           send="gem_send_on", send_threshold=0.88, newchat="gem_newchat")

    def run_wait(self, views, timeout=60.0):
        clock = Clock()
        conv = FakeConversation(views)
        with mock.patch.object(gemini, "Conversation", return_value=conv), \
                mock.patch.object(gemini, "_submit", return_value=None), \
                mock.patch.object(gemini, "Screen", return_value=StubScreen()), \
                mock.patch.object(gemini, "time", clock):
            result = gemini._send_and_wait(StubScreen(), self.SITE, "只回复 OK-WEB2", timeout)
        return result, clock

    def test_complete_reply_is_judged_within_30_seconds(self):
        """回复已经在页面上：判完成要秒级，不能拖到超时。"""
        prompt, reply = "只回复 OK-WEB2", "OK-WEB2"
        answer = flat("T:你说", f"T:{prompt}", "B:复制提示",
                      "T:Gemini 说", f"T:{reply}", "B:答得好", "B:复制")
        result, clock = self.run_wait([
            view(flat("T:说说 Gemini")), view(flat("T:说说 Gemini"), value=prompt),
            view(answer),
        ])

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], reply)
        self.assertLess(clock.now, 30.0, "已完成也要在 30 秒内判定")
        self.assertEqual(result["basis"], "tree_reply_bar_and_stable_text")

    def test_prompt_that_cannot_be_claimed_still_gets_the_new_reply(self):
        """10:35 那次：认领不到提问，但页面上确实多了一条带动作条的回复。"""
        prompt, reply = "只回复 OK-WEB2", "OK-WEB2"
        root = flat("T:Gemini 说", f"T:{reply}", "B:答得好")
        result, _ = self.run_wait([
            view(flat("T:问问 Gemini")), view(flat("T:问问 Gemini"), value=prompt), view(root),
        ])

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], reply)
        self.assertEqual(result.get("attribution"), "new_reply")

    def test_final_read_returns_the_reply_instead_of_no_reply(self):
        """轮询期间一直读不到提问/回复，超时前最后再读一次要能救回来。"""
        prompt, reply = "只回复 OK-WEB2", "OK-WEB2"
        done_tree = flat("T:你说", f"T:{prompt}", "B:复制提示",
                         "T:Gemini 说", f"T:{reply}", "B:答得好")

        result, _ = self.run_wait([
            view(flat("T:问问 Gemini")), view(flat("T:问问 Gemini"), value=prompt),
            view(flat("T:问问 Gemini")), view(done_tree),
        ], timeout=3.0)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["basis"], "final_read")
        self.assertEqual(result["reply"], reply)

    def test_final_read_refuses_a_half_written_answer(self):
        """还在生成（有停止键）时，最后一次读取也不能把半截话当答案。"""
        half = flat("T:Gemini 说", "T:写到一半")
        result, _ = self.run_wait([
            view(flat("T:问问 Gemini")), view(flat("T:问问 Gemini"), value="只回复 OK-WEB2"),
            view(flat("T:问问 Gemini")), view(half, busy=True),
        ], timeout=3.0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "timeout")


class GrokWait(unittest.TestCase):
    """Grok 同一条兜底：认领不到提问也要能把回复收回来。"""

    SITE = SimpleNamespace(name="grok", title_hint="Google Chrome", on_secondary=True,
                           send="grok_send_on", send_threshold=0.90, newchat="grok_newchat",
                           sidebar="grok_sidebar")

    def setUp(self):
        self.use = load_use("grok_use.py")

    def run_wait(self, views, timeout=3.0):
        clock = Clock()
        conv = FakeConversation(views)
        with mock.patch.object(self.use, "Conversation", return_value=conv), \
                mock.patch.object(self.use, "_submit", return_value="uia"), \
                mock.patch.object(self.use, "time", clock):
            result = self.use._send_and_wait(StubScreen(), self.SITE, "只回复 OK-WEB2", timeout)
        return result, clock

    def test_final_read(self):
        prompt, reply = "只回复 OK-WEB2", "OK-WEB2"
        done_tree = flat("T:你", f"T:{prompt}", "B:Edit", "T:Grok", f"T:{reply}", "B:复制响应")

        result, _ = self.run_wait([
            view(flat("E:向 Grok 提任何问题")), view(flat("E:向 Grok 提任何问题"), value=prompt),
            view(flat("E:向 Grok 提任何问题")), view(done_tree),
        ])

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["basis"], "final_read")
        self.assertEqual(result["reply"], reply)
        self.assertEqual(result["submit"], "uia")


class WriteVerify(unittest.TestCase):
    """输入框没吃进去一次，要能自己纠正，而不是整轮作废（10:22 的 input_mismatch）。"""

    PROMPT = "只回复 OK-WEB2"

    def test_grok_retries_after_a_missed_click(self):
        use = load_use("grok_use.py")
        conv = FakeConversation([
            view(flat("T:x"), value=""), view(flat("T:x"), value=""),
            view(flat("T:x"), value=self.PROMPT),
        ])
        screen = SimpleNamespace(hotkey=mock.Mock(), press=mock.Mock())
        with mock.patch.object(use.time, "sleep"):
            written = use.write_prompt(screen, conv, view(flat("T:x")), self.PROMPT)

        self.assertEqual(written.value, self.PROMPT)
        self.assertEqual(len(conv.writes), 2, "第一次没写进去，应当再试一次")
        screen.hotkey.assert_called_with("ctrl", "a")

    def test_grok_gives_up_with_the_last_value(self):
        use = load_use("grok_use.py")
        conv = FakeConversation([view(flat("T:x"), value="")])
        screen = SimpleNamespace(hotkey=mock.Mock(), press=mock.Mock())
        with mock.patch.object(use.time, "sleep"):
            written = use.write_prompt(screen, conv, view(flat("T:x")), self.PROMPT,
                                       tries=2)
        self.assertNotEqual(written.value, self.PROMPT, "没写进去就不能说成功")

    def test_gemini_retries_after_a_missed_click(self):
        conv = FakeConversation([
            view(flat("T:x"), value=""), view(flat("T:x"), value=""),
            view(flat("T:x"), value=self.PROMPT),
        ])
        screen = SimpleNamespace(hotkey=mock.Mock(), press=mock.Mock())
        with mock.patch.object(gemini.time, "sleep"):
            written = gemini.write_prompt(screen, conv, view(flat("T:x")), self.PROMPT)

        self.assertEqual(written.value, self.PROMPT)
        self.assertEqual(len(conv.writes), 2)


# ---------------------------------------------------------------- 5. grok --read
class GrokRead(unittest.TestCase):
    """`grok_use --read`：不发送、不点按钮，输出格式和 gemini 一致。"""

    def setUp(self):
        self.use = load_use("grok_use.py")
        self.tree = flat("T:你", "T:只回复 OK-WEB2", "B:Edit", "T:Grok", "T:OK-WEB2", "B:复制响应")

    def patches(self, tree=None):
        view_ = view(tree or self.tree, url="https://grok.com/c/abc")
        screen = SimpleNamespace(bind=lambda hwnd: True, focus=lambda: True,
                                 click=lambda *a, **k: (_ for _ in ()).throw(AssertionError("只读不许点")))

        class FakeConv:
            def __init__(self, *args, **kwargs):
                pass

            def read(self, *args, **kwargs):
                return view_

        return [
            mock.patch.object(self.use, "ensure_site", return_value=123),
            mock.patch.object(self.use, "Screen", return_value=screen),
            mock.patch.object(self.use, "Conversation", FakeConv),
        ]

    def test_read_returns_the_last_reply(self):
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            result = self.use.read_last(verbose=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "read")
        self.assertEqual(result["reply"], "OK-WEB2")
        self.assertEqual(result["replies"], 1)
        self.assertIsNone(result["submission"])
        self.assertEqual(result["url"], "https://grok.com/c/abc")

    def test_read_says_no_reply_on_an_empty_page(self):
        with contextlib.ExitStack() as stack:
            for p in self.patches(flat("E:向 Grok 提任何问题", "T:畅所欲问")):
                stack.enter_context(p)
            result = self.use.read_last(verbose=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "no_reply")

    def test_cli_read_prints_json_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(self.use.guard, "guard", return_value=True))
            stack.enter_context(mock.patch("qi.runlog.record"))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = self.use.cli(self.use.SITE, ["--read"])

        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue().strip())
        self.assertEqual(printed["stage"], "read")
        self.assertEqual(printed["reply"], "OK-WEB2")

    def test_read_rejects_a_prompt(self):
        with mock.patch.object(self.use.guard, "guard", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.use.cli(self.use.SITE, ["--read", "一句话"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_prompt_without_read_is_still_an_error(self):
        with mock.patch.object(self.use.guard, "guard", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.use.cli(self.use.SITE, [])
        self.assertEqual(ctx.exception.code, 2)


class GrokMain(unittest.TestCase):
    """grok_use.py 这一层的接线：--read 不能被模板检查挡住。"""

    def setUp(self):
        self.use = load_use("grok_use.py")

    def run_main(self, argv):
        with mock.patch.object(sys, "argv", ["grok_use.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            return self.use.main()

    def test_read_skips_library_check(self):
        with mock.patch.object(self.use, "Library") as library, \
                mock.patch.object(self.use, "cli", return_value=0) as cli:
            self.assertEqual(self.run_main(["--read", "--tab", "2"]), 0)
        library.assert_not_called()
        self.assertEqual(cli.call_args.args[1], ["--read", "--tab", "2"])

    def test_sending_still_checks_templates(self):
        with mock.patch.object(self.use, "Library") as library, \
                mock.patch.object(self.use, "cli", return_value=0):
            library.return_value.missing.return_value = []
            library.return_value.report.return_value = "资源库就绪：5/5 张"
            self.assertEqual(self.run_main(["收到"]), 0)
        need = library.return_value.report.call_args.args[0]
        self.assertIn("grok_send_on", need)
        self.assertIn("grok_newchat", need)


if __name__ == "__main__":
    unittest.main()
