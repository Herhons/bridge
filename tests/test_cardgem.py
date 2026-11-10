"""任务卡 GEM：gemini_use 认不出已经完成的回复（11:4x 真机超时）。

夹具就是那次现场抓下来的真实控件树 `evidence/gemini_bug_1215_uia.txt`：
提问气泡是**折叠**的，文本前面粘着标签「你说」、末尾是一个「…」（旁边有「展开」）。
旧判据要求整串精确相等，在这份树上命中 0 条 → `accepted` 永远不成立 →
`submission` 停在 unknown → 240s 后报 timeout/no_reply，而回复早就写完了。

这里钉四件事：
1. 旧判据在这份树上确实一条都不中（根因，防止有人改回去）；
2. 新判据能从同一份树里认领提问、读出回复、读到 VERDICT 行；
3. 完成判据按"这条回复自己的动作条"算，别的轮次的动作条不算数；
4. `--read` 不发人、不点按钮，直接把最后一条回复打出来。

全部离线：不开窗口、不截图、不点击。
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
from qi.conversation import prompt_nodes  # noqa: E402
from qi.screen import Match  # noqa: E402

FIXTURE = ROOT / "evidence" / "gemini_bug_1215_uia.txt"
KINDS = {"B": "ButtonControl", "T": "TextControl"}


def flat(*lines: str) -> ui.Node:
    """把 "T:xxx" / "B:xxx" 这种平铺行搭成一棵树（文档序＝行序）。"""
    root = ui.Node("DocumentControl", "")
    for line in lines:
        if len(line) < 2 or line[1] != ":":
            continue
        kind, name = line[0], line[2:]
        if name:
            root.children.append(ui.Node(KINDS.get(kind, "TextControl"), name, None, root))
    return root


def real_tree() -> ui.Node:
    """真实控件树。原始 dump 是平铺的，文档序就是行序，够本模块用。"""
    return flat(*FIXTURE.read_text(encoding="utf-8-sig").splitlines())


def load_use():
    """按路径装 gemini_use.py（它不在包里，但会 import 项目代码）。"""
    name = "cardgem_gemini_use"
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / "scripts" / "gemini_use.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RealTree(unittest.TestCase):
    """11:4x 那份现场控件树。"""

    @classmethod
    def setUpClass(cls):
        cls.root = real_tree()
        cls.scan = gemini.scan(cls.root)
        cls.prompts = [b for b in cls.scan.blocks if b.label in gemini.PROMPT_LABELS]
        cls.replies = cls.scan.replies()

    def test_fixture_is_not_empty(self):
        self.assertTrue(FIXTURE.exists(), f"缺现场控件树：{FIXTURE}")
        self.assertGreater(len(self.scan.blocks), 2, "一个气泡都没切出来")

    def test_old_exact_match_misses_the_collapsed_bubble(self):
        """根因：树里那条折叠气泡只剩前两行，末尾是「…」。

        旧判据要求"某个节点的文本 == 整条提问"，折叠气泡永远对不上——真机上
        的直接表现就是 `submission` 240s 一直停在 unknown（accepted 从没成立）。
        """
        truncated = next(b for b in self.prompts if b.text.endswith("…"))
        sent = truncated.text + "后面还有一段没渲染出来的内容"
        self.assertEqual(len(prompt_nodes(SimpleNamespace(root=self.root), sent)), 0,
                         "旧判据在这里应当一条都不中")
        self.assertIsNotNone(self.scan.prompt_at(sent), "新判据应当认得出这是同一条")

    def test_prompt_is_claimed_despite_collapse(self):
        longest = max(self.prompts, key=lambda b: len(b.text))
        self.assertTrue(longest.text.strip().startswith("你是只读评审"), "认错了提问气泡")
        self.assertGreater(len(longest.text), 200, "只读到第一行，块被提前截断了")
        # 树里只渲染了前一段（我们发的更长）也要认得出
        self.assertIsNotNone(self.scan.prompt_at(longest.text + "（后面还有一段没渲染出来）"))
        # 折叠那条（尾部「…」）同样要认得出
        truncated = [b for b in self.prompts if b.text.endswith("…")]
        self.assertIsNotNone(self.scan.prompt_at(truncated[0].text))

    def test_truncated_prefix_still_matches(self):
        """末尾省略号、以及树里只渲染了前一段，都要认得出是同一条提问。"""
        longest = max(self.prompts, key=lambda b: len(b.text))
        self.assertTrue(gemini.prompt_matches(longest.text + "…", longest.text))
        self.assertTrue(gemini.prompt_matches(longest.text[:60], longest.text))
        self.assertFalse(gemini.prompt_matches("无关的一段话" * 5, longest.text))

    def test_reply_text_is_readable_with_verdict_line(self):
        self.assertTrue(self.replies, "没切出任何回复")
        text = ui.canonical(self.replies[-1].text)
        self.assertIn("VERDICT:REVISE", text)
        self.assertIn("Gemini 说", [b.label for b in self.replies])

    def test_reply_keeps_original_spacing(self):
        """回复要原样交出去：词与词之间的空格是靠节点自己的前后空格拼回来的。"""
        self.assertIn("仅限 PLAN.md 和 CHANGES-20261030.md", self.replies[-1].text)

    def test_reply_is_after_our_prompt(self):
        longest = max(self.prompts, key=lambda b: len(b.text))
        at = self.scan.prompt_at(longest.text)
        reply = self.scan.reply_after(at)
        self.assertIsNotNone(reply, "我们这条提问后面没有回复")
        self.assertIs(reply, self.replies[-1])

    def test_done_bar_belongs_to_the_reply(self):
        self.assertTrue(self.scan.done(self.replies[-1]))
        # 提问条是 [展开, 复制提示, 修改]，不能算完成
        prompt = max(self.prompts, key=lambda b: len(b.text))
        self.assertFalse(self.scan.done(prompt))


class Blocks(unittest.TestCase):
    """切块的边角：标签不能被正文里的字骗到，动作条要归到前一块。"""

    def test_label_inside_reply_is_just_text(self):
        blocks, _ = gemini.parse(flat("T:Gemini 说", "T:你说的对，我同意", "B:答得好"))
        reply = blocks[-1]
        self.assertEqual(reply.label, "Gemini 说")
        self.assertEqual(reply.text, "你说的对，我同意")

    def test_duplicate_label_node_is_one_block(self):
        blocks, _ = gemini.parse(flat("T:Gemini 说", "T:Gemini 说", "T:正文", "B:复制"))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "正文")

    def test_editor_closes_the_block(self):
        """输入框一到就断块：用户刚打的字不能被当成回复正文。"""
        blocks, _ = gemini.parse(flat("T:Gemini 说", "T:正文", "T:问问 Gemini"))
        self.assertEqual(blocks[0].text, "正文")

    def test_doubled_prompt_node_is_squashed(self):
        """真机 04:2x：用户气泡把同一句话挂了两个节点（同屏、同文本）。

        不挡掉就会把提问读成两遍（len=34），前缀比对失效，又是 240s 超时。
        """
        prompt = "只回复这一行：GEM-2601 OK"
        root = flat("T:你说", f"T:{prompt}", f"T:{prompt}", "B:复制提示",
                    "T:Gemini 说", "T:GEM-2601 OK", "B:答得好")
        scan = gemini.scan(root)
        self.assertEqual(scan.blocks[0].key, ui.canonical(prompt))
        self.assertEqual(scan.prompt_at(prompt), 0)
        # 正文保真：空格不能被判据用的规范形抹掉
        self.assertEqual(scan.replies()[0].text, "GEM-2601 OK")

    def test_whole_block_repeated_twice_still_matches(self):
        """两个重复节点不相邻时 parse 挡不住，匹配这一层再兜一次。"""
        prompt = "只回复这一行：GEM-2601 OK"
        self.assertTrue(gemini.prompt_matches(prompt * 2, prompt))

    def test_old_reply_is_not_taken_for_this_turn(self):
        root = flat("T:你说", "T:上一轮的提问", "T:Gemini 说", "T:上一轮的回答", "B:复制",
                    "T:你说", "T:这一轮的提问，足够长足够长足够长", "T:Gemini 说",
                    "T:这一轮的回答", "B:答得好")
        scan = gemini.scan(root)
        at = scan.prompt_at("这一轮的提问，足够长足够长足够长")
        self.assertIsNotNone(at)
        self.assertEqual(scan.reply_after(at).text, "这一轮的回答")

    def test_generating_reply_is_not_done(self):
        """还没有动作条的回复＝还在生成，不能被判完成。"""
        scan = gemini.scan(flat("T:你说", "T:提问内容足够长足够长足够长", "T:Gemini 说", "T:写了一半"))
        reply = scan.reply_after(scan.prompt_at("提问内容足够长足够长足够长"))
        self.assertIsNotNone(reply)
        self.assertFalse(scan.done(reply))

    def test_done_accepts_any_done_button_in_the_bar(self):
        """动作条的按钮顺序按界面版本会变：不能只认第一个按钮。"""
        scan = gemini.scan(flat("T:你说", "T:提问内容足够长足够长足够长", "T:Gemini 说", "T:回答",
                                "B:重做", "B:复制", "B:显示更多选项"))
        reply = scan.reply_after(scan.prompt_at("提问内容足够长足够长足够长"))
        self.assertTrue(scan.done(reply))
        self.assertEqual(reply.bars, ["重做", "复制", "显示更多选项"])


class Submit(unittest.TestCase):
    """发送：先按控件名点真按钮，模板只在没按钮时兜底。"""

    class Ctrl:
        IsEnabled = True
        BoundingRectangle = SimpleNamespace(left=0, top=0, right=20, bottom=20)

    class StubScreen:
        def __init__(self, match=None):
            self.match = match
            self.clicks = []
            self.find_calls = []

        def click_xy(self, x, y):
            self.clicks.append((x, y))

        def click(self, m, *, offset=(0, 0)):
            self.clicks.append(m.center)

        def find(self, name, *, threshold=None, region=None):
            self.find_calls.append(name)
            return self.match

        def window_rect(self, *args, **kwargs):
            return (0, 0, 100, 100)

    def site(self):
        return SimpleNamespace(name="gemini", title_hint="Google Chrome", on_secondary=True,
                               send="gem_send_on", send_threshold=0.88)

    def view(self, *lines):
        root = flat(*lines)
        for node in root.walk():
            node.control = self.Ctrl() if node.kind == "ButtonControl" else None
        return SimpleNamespace(root=root)

    def test_named_send_button_wins(self):
        screen = self.StubScreen()
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        gemini._submit(screen, self.site(), conv, self.view("B:发送"))
        self.assertEqual(screen.clicks, [(10, 10)])
        self.assertEqual(screen.find_calls, [], "有真按钮时不该去截图找模板")

    def test_template_is_the_fallback(self):
        match = Match("gem_send_on", 0.93, 100, 200, 10, 10)
        screen = self.StubScreen(match)
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        with mock.patch("time.sleep"):
            gemini._submit(screen, self.site(), conv, self.view("T:只有文字没有按钮"))
        self.assertEqual(screen.find_calls, ["gem_send_on"])
        self.assertEqual(screen.clicks, [(105, 205)])

    def test_no_send_key_at_all_is_an_error(self):
        screen = self.StubScreen(None)
        conv = SimpleNamespace(read=lambda *a, **k: self.view())
        with mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                gemini._submit(screen, self.site(), conv, self.view("T:什么都没有"))
        self.assertIn("发送键没找到", str(ctx.exception))


class ReadOnly(unittest.TestCase):
    """`--read`：只读，不发送、不点按钮。"""

    def fake(self, reply_tree):
        view = SimpleNamespace(root=reply_tree, url="https://gemini.google.com/app/abc",
                               editor=None, value="", busy=False, copies=[])

        class FakeConversation:
            def __init__(self, *args, **kwargs):
                pass

            def read(self, *args, **kwargs):
                return view

        screen = SimpleNamespace(bind=lambda hwnd: True, focus=lambda: True)
        patches = [
            mock.patch.object(gemini, "ensure_site", return_value=123),
            mock.patch.object(gemini, "Screen", return_value=screen),
            mock.patch.object(gemini, "Conversation", FakeConversation),
        ]
        return patches

    def test_read_last_returns_the_real_reply(self):
        with contextlib.ExitStack() as stack:
            for p in self.fake(real_tree()):
                stack.enter_context(p)
            result = gemini.read_last(verbose=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "read")
        self.assertIn("VERDICT:REVISE", ui.canonical(result["reply"]))

    def test_read_last_says_no_reply_when_page_is_empty(self):
        with contextlib.ExitStack() as stack:
            for p in self.fake(flat("T:发起新对话", "T:问问 Gemini")):
                stack.enter_context(p)
            result = gemini.read_last(verbose=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "no_reply")

    def test_cli_read_prints_json_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in self.fake(real_tree()):
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(gemini.guard, "guard", return_value=True))
            stack.enter_context(mock.patch("qi.runlog.record"))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = gemini.cli(SimpleNamespace(name="gemini", title_hint="Google Chrome",
                                              on_secondary=True, send="gem_send_on",
                                              send_threshold=0.88), ["--read"])
        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue().strip())
        self.assertEqual(printed["stage"], "read")
        self.assertIn("VERDICT:REVISE", ui.canonical(printed["reply"]))

    def test_cli_rejects_prompt_with_read(self):
        site = SimpleNamespace(name="gemini", title_hint="Google Chrome",
                               on_secondary=True, send="gem_send_on", send_threshold=0.88)
        with mock.patch.object(gemini.guard, "guard", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                gemini.cli(site, ["--read", "一句话"])
        self.assertEqual(ctx.exception.code, 2)


class UseScript(unittest.TestCase):
    """gemini_use.py 这层的接线：--read 不能被模板检查挡住。"""

    def run_main(self, module, argv):
        with mock.patch.object(sys, "argv", ["gemini_use.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def test_read_skips_library_check(self):
        module = load_use()
        with mock.patch.object(module, "Library") as library, \
                mock.patch.object(module.gemini, "cli", return_value=0) as cli:
            self.assertEqual(self.run_main(module, ["--read"]), 0)
        library.assert_not_called()
        self.assertEqual(cli.call_args.args[1], ["--read"])

    def test_sending_still_checks_templates(self):
        module = load_use()
        with mock.patch.object(module, "Library") as library, \
                mock.patch.object(module.gemini, "cli", return_value=0):
            library.return_value.missing.return_value = []
            library.return_value.report.return_value = "资源库就绪：7/7 张"
            self.assertEqual(self.run_main(module, ["收到"]), 0)
        need = library.return_value.report.call_args.args[0]
        self.assertIn("gem_send_on", need)
        self.assertIn("gem_newchat", need)


if __name__ == "__main__":
    unittest.main()

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/gemini_bug_1215_uia.txt',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
