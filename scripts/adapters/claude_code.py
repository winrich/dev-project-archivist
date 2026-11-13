#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code 适配器。

数据源（只读）：
    ~/.claude/projects/<路径编码>/<会话UUID>.jsonl

路径编码规则：把项目绝对路径里所有非字母数字字符替换为 `-`，
例如 /Users/me/shop-api → -Users-me-shop-api。

转录格式（逐行 JSON，append-only）：
    顶层字段   type / uuid / parentUuid / timestamp / sessionId / cwd / gitBranch / version
    type 取值  user / assistant / custom-title / ai-title / summary / system /
               attachment / queue-operation / pr-link / result / file-history-snapshot
    assistant  message.content 为块数组：text / thinking / tool_use{id,name,input}
    user       message.content 为块数组：text / tool_result{tool_use_id,content}

几个必须处理的坑（均来自实测经验，非推测）：
  1. content 永远是数组，单个文本也是数组；但为容错仍兼容字符串。
  2. tool_result 的 content 可能是字符串或数组，两种都要接。
  3. 子代理记录带 isSidechain=true，混在主文件里，必须过滤。
  4. 用户消息里混有 isMeta 记录、`[Request interrupted` 中断标记，不算真实提问。
  5. 同一条 assistant 消息按内容块逐行写入，usage 会重复；本适配器不读 usage，故无影响。
  6. 时间戳是 ISO 8601 字符串，但索引层必须转成毫秒整数，否则与 WorkBuddy 混排会崩。
"""

import json
import os
from pathlib import Path

from .base import (Adapter, ERROR_HINT, EV_ASSISTANT, EV_PROMPT, EV_TITLE,
                   EV_TOOL, EV_TOOL_RESULT, EV_TS, EV_WRITE, Index,
                   clean_prompt, iso_to_ms, rel_in_cwd)

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# 读取会话头部时最多扫多少行来定位 cwd / 标题 / 起始时间
HEAD_SCAN_LINES = 60
# 会话里可视为「写入完整文件」的工具名（Edit 只有片段，无法还原全文，不记内容）
WRITE_TOOLS = ("Write",)
# 这些 type 只承载元信息，不产生对话事件
SKIP_TYPES = ("summary", "system", "attachment", "queue-operation", "pr-link",
              "result", "file-history-snapshot")


def _block_text(content) -> str:
    """content 可能是字符串或块数组，统一取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(i.get("text", "") for i in content
                        if isinstance(i, dict) and isinstance(i.get("text"), str))
    return ""


def _iter_lines(path):
    """逐行产出已解析的 JSON 对象；解析失败的行静默跳过（半行写入是常态）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    yield o
    except Exception as e:
        print(f"  [warn] 解析失败 {Path(path).name}: {e}", flush=True)


# ---------------------------------------------------------------- 会话索引
class ClaudeCodeIndex(Index):
    """以文件名（会话 UUID）为 sid 的索引。"""

    def __init__(self):
        self.meta = {}
        self.paths = {}

    def load(self):
        if not PROJECTS_DIR.exists():
            return self
        for p in PROJECTS_DIR.glob("*/*.jsonl"):
            sid = p.stem
            meta = self._probe_session(p, sid)
            if meta:
                self.meta[sid] = meta
                self.paths[sid] = p
        return self

    def _probe_session(self, path, sid):
        """只读文件头部若干行，拿到 cwd、标题、起始时间，避免整文件扫描。"""
        cwd = title = ""
        created = None
        first_user = ""
        for i, o in enumerate(_iter_lines(path)):
            if i >= HEAD_SCAN_LINES:
                break
            if not cwd:
                cwd = o.get("cwd") or ""
            if created is None:
                created = iso_to_ms(o.get("timestamp"))
            t = o.get("type")
            if not title and t in ("custom-title", "ai-title"):
                title = (o.get("title") or "").strip()
            if not first_user and t == "user" and not o.get("isMeta"):
                msg = o.get("message") or {}
                txt = _block_text(msg.get("content") if isinstance(msg, dict) else "")
                if txt and not txt.startswith("[Request interrupted"):
                    first_user = clean_prompt(txt)[:60]
            if cwd and created and title:
                break
        if not cwd:
            return None
        if not title:
            title = first_user
        try:
            mtime = int(os.stat(path).st_mtime * 1000)
        except Exception:
            mtime = created
        return {
            "cwd": cwd,
            "title": title,
            "created": created or mtime,
            "updated": mtime,
            "model": "",
            "playground": False,
            "automation": False,
            "source": "claude-code",
        }

    def ids(self):
        return set(self.meta)

    def info(self, sid):
        return self.meta.get(sid, {})

    def transcript(self, sid):
        return self.paths.get(sid)


# ---------------------------------------------------------------- 适配器
class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    display = "Claude Code"

    def probe(self) -> bool:
        return PROJECTS_DIR.exists()

    def load_index(self):
        return ClaudeCodeIndex().load()

    def iter_events(self, path, cwd: str = ""):
        for o in _iter_lines(path):
            if o.get("isSidechain"):
                continue          # 子代理的记录不属于主对话
            ts = o.get("timestamp")
            if ts:
                yield {"kind": EV_TS, "value": ts}
            t = o.get("type")
            if t in ("custom-title", "ai-title"):
                ti = (o.get("title") or "").strip()
                if ti:
                    yield {"kind": EV_TITLE, "text": ti}
            elif t == "user":
                for ev in self._user_events(o):
                    yield ev
            elif t == "assistant":
                for ev in self._assistant_events(o, cwd):
                    yield ev
            elif t in SKIP_TYPES:
                continue

    # ---------- 单条记录 → 事件
    def _user_events(self, o):
        if o.get("isMeta"):
            return
        msg = o.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            return
        for b in blocks:
            if not isinstance(b, dict):
                continue
            ty = b.get("type")
            if ty == "text":
                raw = b.get("text") or ""
                if not raw or raw.startswith("[Request interrupted"):
                    continue
                txt = clean_prompt(raw)
                if txt:
                    yield {"kind": EV_PROMPT, "text": txt}
            elif ty == "tool_result":
                txt = _block_text(b.get("content")).strip().replace("\n", " ")
                if txt and ERROR_HINT.search(txt[:1500]):
                    yield {"kind": EV_TOOL_RESULT, "text": txt[:220]}

    def _assistant_events(self, o, cwd):
        msg = o.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            ty = b.get("type")
            if ty == "text":
                txt = b.get("text") or ""
                if txt:
                    yield {"kind": EV_ASSISTANT, "text": txt}
            elif ty == "tool_use":
                name = b.get("name") or ""
                yield {"kind": EV_TOOL, "name": name}
                if name not in WRITE_TOOLS:
                    continue
                inp = b.get("input")
                if not isinstance(inp, dict):
                    continue
                fp = inp.get("file_path") or inp.get("path") or inp.get("target_file") or ""
                rel = rel_in_cwd(fp, cwd)
                if not rel:
                    continue
                body = inp.get("content")
                if isinstance(body, str) and body:
                    yield {"kind": EV_WRITE, "rel": rel, "content": body}

    # ---------- 回跳与补充信息
    def deep_link(self, sid: str) -> str:
        # Claude Code 没有 URL scheme，只能给出可执行的恢复命令
        return ""

    def open_hint(self) -> str:
        return "在项目目录执行 `claude --resume <会话ID>`"

    def memory_dir(self, workspace):
        # Claude Code 用 CLAUDE.md 而非记忆目录，无对应概念
        return None

    def workspace_root(self):
        # 用户自己的项目目录分布在各处，没有统一根
        return None
