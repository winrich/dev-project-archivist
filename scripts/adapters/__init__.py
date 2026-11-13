#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器注册表。

新增平台只需两步：
    1. 在本目录新建 <name>.py，实现 Adapter 子类；
    2. 在本文件末尾 register(YourAdapter)。
主程序与其余归档逻辑无需任何改动。
"""

from .base import Adapter, Index, MultiIndex  # noqa: F401  （对外暴露）

REGISTRY = {}


def register(cls):
    """注册一个适配器类。"""
    inst = cls()
    REGISTRY[inst.name] = inst
    return cls


def get_adapter(name: str = "workbuddy") -> Adapter:
    """按名称取适配器；名称不存在时退回第一个可用的。"""
    if name in REGISTRY:
        return REGISTRY[name]
    for a in REGISTRY.values():
        if a.probe():
            return a
    return REGISTRY.get("workbuddy") or next(iter(REGISTRY.values()))


def get_adapters(names=None) -> list:
    """按名称取适配器列表。

    names 为空或含 "all" 时返回本机探测到数据的全部适配器；
    显式指定的适配器即使探测不到也保留（便于用测试数据驱动）。
    """
    if not names or "all" in names:
        found = [a for a in REGISTRY.values() if a.probe()]
        return found or [get_adapter()]
    out = [REGISTRY[n] for n in names if n in REGISTRY]
    return out or [get_adapter()]


def build_index(adapters) -> MultiIndex:
    """把若干适配器的索引聚合为一个，供主程序统一使用。"""
    return MultiIndex([(a.name, a.load_index()) for a in adapters])


def available() -> list:
    """返回本机探测到数据的适配器名称列表（按注册顺序）。"""
    return [n for n, a in REGISTRY.items() if a.probe()]


# ---------------------------------------------------------------- 内置适配器
from .claude_code import ClaudeCodeAdapter  # noqa: E402
from .workbuddy import WorkBuddyAdapter  # noqa: E402

register(WorkBuddyAdapter)
register(ClaudeCodeAdapter)
