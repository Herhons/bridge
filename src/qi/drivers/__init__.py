"""端驱动协议（S4 骨架，本轮只定义接口，不迁实现）。

目标架构：**一个总控 + 各端无状态驱动**。
总控（`tools/sisyphus.py`）独占 UI 锁、会话锚点、账本、超时与硬停判定；
各端驱动只做"原子动作"，不自己重试、不管锁、不写账本。

迁移顺序（下一轮起）：
  1. `*_use.py` / `*_choose.py` 退化为 CLI 薄壳，内部改调驱动；
  2. 先把 gemini 接进来（它已经有 `upload-script` 这条最稳的路径）；
  3. 再迁 deepseek / grok / chatgpt；
  4. 最后删除旧的并行发送栈。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SiteDriver(Protocol):
    name: str

    def ensure_session(self, *, round_name: str, prompt_hash: str) -> dict:
        """确保当前页面是本轮会话；返回 {action, url}，不得自己新建。"""

    def choose_model(self, purpose: str) -> dict:
        """选档并返回回执摘要 {ok, model}。"""

    def upload(self, path: str) -> dict:
        """上传附件；返回 {ok, filename}。"""

    def submit(self, text: str) -> dict:
        """写入并提交；返回 {ok, stage, submission}。"""

    def read_reply(self) -> dict:
        """只读取回复；返回 {ok, reply}。"""
