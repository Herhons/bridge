"""Registry of all 11 standalone Bridge v0.2.5 endpoint drivers."""
from __future__ import annotations

from endpoints.antigravity import AntigravityDriver
from endpoints.chatgpt import ChatGPTDriver
from endpoints.codex import CodexDriver
from endpoints.deepseek import DeepSeekDriver
from endpoints.dsh import DSHDriver
from endpoints.gemini import GeminiDriver
from endpoints.grok import GrokDriver
from endpoints.grokbot import GrokbotDriver
from endpoints.hermes import HermesDriver
from endpoints.opencode import OpenCodeDriver
from endpoints.workbuddy import WorkBuddyDriver

REGISTRY = {
    "chatgpt": ChatGPTDriver,
    "gemini": GeminiDriver,
    "grok": GrokDriver,
    "deepseek": DeepSeekDriver,
    "codex": CodexDriver,
    "dsh": DSHDriver,
    "opencode": OpenCodeDriver,
    "workbuddy": WorkBuddyDriver,
    "hermes": HermesDriver,
    "grokbot": GrokbotDriver,
    "antigravity": AntigravityDriver,
}

ENDPOINT_NAMES = tuple(REGISTRY.keys())


def get_driver(name: str, **kwargs):
    key = name.strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"Unknown endpoint: {name!r}. Available: {', '.join(ENDPOINT_NAMES)}")
    return REGISTRY[key](**kwargs)
