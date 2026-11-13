#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器基类与中立事件规范。

主程序（archiver.py）只认这里定义的 7 种事件，不认任何平台的 JSON 结构。
新增一个平台 = 新增一个 Adapter 子类 + 在 __init__.py 注册，主程序零改动。

事件规范（均为 dict，kind 为必填项）：

    {"kind": "ts",          "value": "2026-11-13T10:42:01Z"}   时间戳
    {"kind": "title",       "text": "会话标题"}                  会话标题
    {"kind": "prompt",      "text": "用户真实提问"}              已剥离注入块的用户提问
    {"kind": "assistant",   "text": "助手输出文本"}              助手正文
    {"kind": "tool",        "name": "Write"}                    工具调用（仅用于画像计数）
    {"kind": "write",       "rel": "src/a.cs", "content": "…"}  已归一为相对路径的写入
    {"kind": "tool_result", "text": "错误线索片段"}              工具输出中的错误线索（非全量）

设计要点：
  1. adapter 负责「平台语义 → 通用语义」：字段名映射、路径相对化、注入块剥离都在 adapter 内完成。
  2. adapter 不做任何统计与判定，只吐事件；所有业务逻辑留在主程序的 SessionParser。
  3. tool_result 只保留错误线索，避免把整段工具输出带进内存（体积控制的原点）。
"""

import re


# ---------------------------------------------------------------- 事件类型
EV_TS = "ts"
EV_TITLE = "title"
EV_PROMPT = "prompt"
EV_ASSISTANT = "assistant"
EV_TOOL = "tool"
EV_WRITE = "write"
EV_TOOL_RESULT = "tool_result"

# 工具输出中的错误线索（通用启发式，不含平台特征）
ERROR_HINT = re.compile(
    r"(错误|失败|异常|不通过|报错|Error|error:|Exception|CS\d{4}|fail)", re.I)


def rel_in_cwd(raw_path: str, cwd: str) -> str:
    """把工具调用里的绝对路径归一为相对工作区的路径。

    只保留落在工作区内的写入；工作区外的写入（改配置文件、写临时目录）一律丢弃。
    返回空串表示「不属于本工作区」。
    """
    norm = (raw_path or "").replace("\\", "/")
    if not norm:
        return ""
    base = (cwd or "").replace("\\", "/").rstrip("/")
    if not base:
        # 没有工作区信息时退化为文件名，与既有行为一致
        return norm.split("/")[-1]
    low, bl = norm.lower(), base.lower()
    if low == bl:
        # 写入路径恰好等于工作区根（实际几乎不会发生），保留原样以维持既有行为
        return norm
    if not low.startswith(bl + "/"):
        return ""
    rel = norm[len(base) + 1:]
    if not rel or rel.startswith(".."):
        return ""
    return rel


# ---------------------------------------------------------------- 索引接口
class Index:
    """统一会话索引：主程序只调用这三个方法，不关心索引从哪来。"""

    def ids(self) -> set:
        return set()

    def info(self, sid: str) -> dict:
        return {}

    def transcript(self, sid: str):
        return None


class MultiIndex(Index):
    """把多个平台的索引聚合成一个。

    sid 在各平台之间理论上不会撞（多为 UUID），一旦撞了则加 `<平台>:` 前缀，
    原始 sid 保留在 _origin 里，transcript 仍能正确取到。
    """

    def __init__(self, pairs=None):
        self.meta = {}
        self._origin = {}   # 聚合后的 sid -> (原 index, 原 sid)

        for name, ix in (pairs or []):
            for sid in ix.ids():
                key = sid
                if key in self.meta:
                    key = f"{name}:{sid}"
                info = dict(ix.info(sid) or {})
                info["source"] = name
                self.meta[key] = info
                self._origin[key] = (ix, sid)

    def ids(self):
        return set(self.meta)

    def info(self, sid):
        return self.meta.get(sid, {})

    def transcript(self, sid):
        pair = self._origin.get(sid)
        return pair[0].transcript(pair[1]) if pair else None


# ---------------------------------------------------------------- 适配器基类
class Adapter:
    """平台适配器基类。

    子类必须覆盖 name / display / probe / load_index / iter_events，
    其余方法都有安全默认值：拿不到就返回空，绝不抛错中断整个归档流程。
    """

    name = ""          # 机器标识，写入 manifest 的 source 字段
    display = ""       # 展示名，出现在 README 与日志里

    # ---------- 探测与索引
    def probe(self) -> bool:
        """本机是否存在该平台的数据。返回 False 时整体跳过，不产生任何告警。"""
        return False

    def load_index(self) -> Index:
        """构建本平台的会话索引。"""
        raise NotImplementedError

    def iter_events(self, path, cwd: str = ""):
        """流式产出中立事件。path 为 transcript 路径，cwd 已转为正斜杠小写。"""
        return iter(())

    # ---------- 回跳与补充信息
    def deep_link(self, sid: str) -> str:
        """指向原始对话的深链。无深链能力时返回空串，由 open_hint 兜底。"""
        return ""

    def open_hint(self) -> str:
        """无深链时的文字说明，写入 README 的「打开原对话」小节。"""
        return f"在 {self.display or self.name} 中打开会话 ID："

    def memory_dir(self, workspace):
        """该工作区下的项目记忆目录。无此概念返回 None。"""
        return None

    def workspace_root(self):
        """本平台默认的工作区根目录。无此概念返回 None。"""
        return None
