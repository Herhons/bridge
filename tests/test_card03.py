"""任务卡 03：`workbuddy_use.py --list` 不再被 prompt 卡在 argparse 阶段。

全部打桩：不建窗口、不读真实 UIA 树、不发消息。只验入口的闸门和 --list 的只读输出。
"""

from offline_guard import ensure_offline
ensure_offline()

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi import ui, workbuddy

MODEL = "DeepSeek-V4.1-Flash"


def control(width=740, height=64, enabled=True, offscreen=False):
    """一个假 UIA 控件：只带 visible_editor / current_model 会读的那几个属性。"""
    return SimpleNamespace(IsEnabled=enabled, IsOffscreen=offscreen,
                           BoundingRectangle=SimpleNamespace(left=0, top=0,
                                                             right=width, bottom=height))


def model_node(name=f"Select model: {MODEL}"):
    return ui.Node("ComboBoxControl", name, control(220, 32))


def editor_node(width=740, height=64):
    return ui.Node("EditControl", "", control(width, height))


def page(*nodes):
    """一棵假文档树（root 本身也是 DocumentControl）。"""
    root = ui.Node("DocumentControl", "", control(1200, 900))
    root.children = list(nodes)
    for node in nodes:
        node.parent = root
    return root


@contextlib.contextmanager
def harness(root=None):
    """把入口外面那圈都打桩：闸门、绑窗口、读控件树、新建任务全部假。
    yield 出来的东西里留着 mock 和捕获的 stdout/stderr，供断言用。
    """
    fake_root = page(model_node(), editor_node()) if root is None else root
    captured = SimpleNamespace(out=io.StringIO(), err=io.StringIO())
    with mock.patch.object(workbuddy.guard, "guard", return_value=True) as gate, \
         mock.patch.object(workbuddy, "open_app",
                           return_value=SimpleNamespace(target_hwnd=1)), \
         mock.patch.object(workbuddy.ui, "document", return_value=(object(), "app://wb")), \
         mock.patch.object(workbuddy.ui, "tree", return_value=fake_root), \
         mock.patch.object(workbuddy, "new_chat") as fresh, \
         contextlib.redirect_stdout(captured.out), \
         contextlib.redirect_stderr(captured.err):
        captured.gate, captured.fresh = gate, fresh
        yield captured


def stdout_json(captured):
    return json.loads(captured.out.getvalue().strip().splitlines()[-1])


class ListParsingTests(unittest.TestCase):
    """卡上点名的 4 种参数情况。"""

    def test_list_without_prompt_parses(self):
        """情况 1：--list 不带 prompt 能过参数解析（改前在这里就退 2）。"""
        _, args = workbuddy.parse_args(["--list"], sending=True)
        self.assertTrue(args.list)
        self.assertIsNone(args.prompt)

    def test_list_with_prompt_parses(self):
        """情况 2：--list 带 prompt 也能过参数解析——不会被当成"缺 prompt"。

        （组合本身仍由 main 里的旧闸门挡下，见 ListBehaviourTests 的说明。）
        """
        _, args = workbuddy.parse_args(["--list", "你好"], sending=True)
        self.assertTrue(args.list)
        self.assertEqual(args.prompt, "你好")

    def test_no_list_and_no_prompt_is_rejected(self):
        """情况 3：不带 --list 也不带 prompt 仍报错，措辞和退出码 2 与改前一致。"""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                workbuddy.parse_args([], sending=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("the following arguments are required: prompt", err.getvalue())

    def test_plain_prompt_parses(self):
        """情况 4：正常带 prompt 能解析，原有选项照旧。

        2026-11-02 CARD-06a：发送必须带 --purpose（无默认），所以这里补上。
        """
        _, args = workbuddy.parse_args(["你好", "--purpose", "test"], sending=True)
        self.assertEqual(args.prompt, "你好")
        self.assertFalse(args.list)
        self.assertEqual(args.timeout, 180)
        self.assertEqual(args.purpose, "test")
        _, args = workbuddy.parse_args(["你好", "--new", "--timeout", "30",
                                        "--purpose", "review"], sending=True)
        self.assertTrue(args.new)
        self.assertEqual(args.timeout, 30)

    def test_sending_without_purpose_is_rejected(self):
        """CARD-06a：--purpose 是发送的必填项，没有默认值。--list 只读，不要求。"""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                workbuddy.parse_args(["你好"], sending=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--purpose", err.getvalue())
        _, args = workbuddy.parse_args(["--list"], sending=True)
        self.assertIsNone(args.purpose)

    def test_model_only_still_needs_prompt_when_sending(self):
        """--model 也救不了：改前这里同样退 2，行为没变。"""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                workbuddy.parse_args(["--model", "GLM-5.3"], sending=True)
        self.assertEqual(ctx.exception.code, 2)


class ListBehaviourTests(unittest.TestCase):
    def test_list_reads_model_and_exits_zero(self):
        """真机要的那条：--list 退出 0，输出当前模型。"""
        with harness() as cap:
            code = workbuddy.main(["--list"], sending=True)
        self.assertEqual(code, 0)
        data = stdout_json(cap)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"], MODEL)

    def test_list_never_creates_a_new_task(self):
        """--list 是只读的：改前 sending=True 会走 new_chat，读一次状态就换掉当前会话。"""
        with harness() as cap:
            code = workbuddy.main(["--list"], sending=True)
        self.assertEqual(code, 0)
        cap.fresh.assert_not_called()

    def test_list_reports_editor_size(self):
        """卡上"读出输入框状态"：报的是 visible_editor 挑中的那个框的宽高。"""
        root = page(model_node(), editor_node(740, 64), editor_node(19, 19))
        with harness(root) as cap:
            self.assertEqual(workbuddy.main(["--list"], sending=True), 0)
        self.assertEqual(stdout_json(cap)["editor"], {"name": "", "width": 740, "height": 64})

    def test_list_survives_missing_editor(self):
        """没有输入框时只把 editor 记成 null，模型照样读、退出码照样 0。"""
        with harness(page(model_node())) as cap:
            code = workbuddy.main(["--list"], sending=True)
        self.assertEqual(code, 0)
        data = stdout_json(cap)
        self.assertEqual(data["model"], MODEL)
        self.assertIsNone(data["editor"])

    def test_list_with_prompt_is_still_refused(self):
        """带 prompt 时旧闸门照旧生效——这条不是本次要改的行为，留个钉子防回退。"""
        with harness() as cap:
            with self.assertRaises(SystemExit) as ctx:
                workbuddy.main(["--list", "你好"], sending=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--list 不能与模型修改或提问同用", cap.err.getvalue())
        cap.gate.assert_not_called()          # 连闸门都没走到，更没碰窗口

    def test_choose_entry_without_args_still_prints_help(self):
        """workbuddy_choose.py 那条路（sending=False）没被改动。"""
        with harness() as cap:
            code = workbuddy.main([], sending=False)
        self.assertEqual(code, 2)
        self.assertIn("usage:", cap.out.getvalue())

    def test_choose_entry_list_still_works(self):
        with harness() as cap:
            code = workbuddy.main(["--list"], sending=False)
        self.assertEqual(code, 0)
        self.assertEqual(stdout_json(cap)["model"], MODEL)

    def test_choose_entry_refuses_list_with_model(self):
        with harness() as cap:
            with self.assertRaises(SystemExit) as ctx:
                workbuddy.main(["--list", "--model", "GLM-5.3"], sending=False)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--list 不能与模型修改或提问同用", cap.err.getvalue())


if __name__ == "__main__":
    unittest.main()
