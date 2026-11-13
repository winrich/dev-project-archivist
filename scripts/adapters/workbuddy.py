#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 适配器。

数据源（均只读）：
    ~/.workbuddy/workbuddy.db                    会话元数据（sessions 表）
    ~/.workbuddy/projects/<cwd编码>/<sid>.jsonl  对话转录
    <workspace>/.workbuddy/memory/*.md           项目记忆

转录格式（逐行 JSON）：
    {"type": "message",        "role": "user|assistant", "content": [{"type": "input_text|output_text", "text": "…"}], "timestamp": "…"}
    {"type": "function_call",  "name": "Write", "arguments": {...}|"{…}", "timestamp": "…"}
    {"type": "function_call_result", "content": [...]|"…"}
    {"type": "ai-title",       "title": "…"}
    {"type": "reasoning" / "file-history-snapshot"}   体积大头且无用，直接跳过
"""

import json
import re
import sqlite3
from pathlib import Path

from .base import (Adapter, ERROR_HINT, EV_ASSISTANT, EV_PROMPT, EV_TITLE,
                   EV_TOOL, EV_TOOL_RESULT, EV_TS, EV_WRITE, Index, rel_in_cwd)

HOME = Path.home()
WB_HOME = HOME / ".workbuddy"
PROJECTS_DIR = WB_HOME / "projects"
SESSION_DB = WB_HOME / "workbuddy.db"
ARTIFACT_INDEX = WB_HOME / "artifact-index"      # 预留：产物索引，当前版本未使用
FILE_HISTORY = WB_HOME / "file-history"          # 预留：文件版本快照，当前版本未使用
WORKSPACES_ROOT = HOME / "WorkBuddy"

# 会话正文里注入的上下文块，需要剥离后才是用户真实提问
_INJECT_BLOCK = re.compile(
    r"<(?:current_time|user_info|identity_context|product_identity|"
    r"tone_and_style|memory|user_memory|project_context|connector-status|"
    r"craft_mode|project_layout|additional-data)[^>]*>")


def clean_prompt(txt: str) -> str:
    """剥掉注入的 system-reminder / 身份上下文，只留用户真实提问。"""
    if not txt:
        return ""
    m = re.search(r"<user_query>(.*?)</user_query>", txt, re.S)
    if m:
        txt = m.group(1)
    else:
        txt = re.sub(r"<system-reminder.*?</system-reminder>", "\n", txt, flags=re.S)
        txt = _INJECT_BLOCK.sub("", txt)
    txt = re.sub(r"<[^>]{1,60}>", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt).strip()
    if len(txt) < 2 or txt.startswith("Caveat:"):
        return ""
    return txt


# ---------------------------------------------------------------- 会话索引
class WorkBuddyIndex(Index):
    """session_id → (元数据, 转录路径) 的统一索引。"""

    def __init__(self):
        self.meta = {}   # sid -> dict(cwd,title,created,updated,model,playground,automation,source)
        self.jsonl = {}  # sid -> Path

    def load(self):
        self._load_db()
        self._load_jsonl_dirs()
        return self

    def _load_db(self):
        if not SESSION_DB.exists():
            return
        try:
            con = sqlite3.connect(f"file:{SESSION_DB.as_posix()}?mode=ro", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, cwd, title, custom_title, created_at, updated_at, "
                "model, is_playground, is_background_automation, deleted_at "
                "FROM sessions"
            ).fetchall()
            for r in rows:
                if r["deleted_at"]:
                    continue
                self.meta[r["id"]] = {
                    "cwd": r["cwd"] or "",
                    "title": (r["custom_title"] or r["title"] or "").strip(),
                    "created": r["created_at"],
                    "updated": r["updated_at"] or r["created_at"],
                    "model": r["model"] or "",
                    "playground": bool(r["is_playground"]),
                    "automation": bool(r["is_background_automation"]),
                    "source": "workbuddy",
                }
            con.close()
        except Exception as e:
            print(f"[warn] 读取会话数据库失败，降级为文件扫描：{e}", flush=True)

    def _load_jsonl_dirs(self):
        if not PROJECTS_DIR.exists():
            return
        for p in PROJECTS_DIR.glob("*/*.jsonl"):
            self.jsonl[p.stem] = p

    def ids(self):
        return set(self.meta) | set(self.jsonl)

    def transcript(self, sid):
        return self.jsonl.get(sid)

    def info(self, sid):
        return self.meta.get(sid, {})


# ---------------------------------------------------------------- 适配器
class WorkBuddyAdapter(Adapter):
    name = "workbuddy"
    display = "WorkBuddy"

    def probe(self) -> bool:
        return SESSION_DB.exists() or PROJECTS_DIR.exists()

    def load_index(self):
        return WorkBuddyIndex().load()

    def iter_events(self, path, cwd: str = ""):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # 体积优化：reasoning / 快照记录占大头且无用，先按子串跳过
                    if '"type":"reasoning"' in line or '"type":"file-history-snapshot"' in line:
                        continue
                    if '"type":"function_call_result"' in line:
                        ev = self._result_event(line)
                        if ev:
                            yield ev
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue  # 会话仍在写入时尾部可能是半行
                    ts = o.get("timestamp")
                    if ts:
                        yield {"kind": EV_TS, "value": ts}
                    t = o.get("type")
                    if t == "ai-title":
                        ti = (o.get("title") or o.get("text") or "").strip()
                        if ti:
                            yield {"kind": EV_TITLE, "text": ti}
                    elif t == "message":
                        for ev in self._message_events(o):
                            yield ev
                    elif t == "function_call":
                        for ev in self._tool_events(o, cwd):
                            yield ev
        except Exception as e:
            print(f"  [warn] 解析失败 {Path(path).name}: {e}", flush=True)

    # ---------- 单条记录 → 事件
    def _message_events(self, o):
        role = o.get("role")
        content = o.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            txt = item.get("text") or ""
            if item.get("type") == "input_text" and role == "user":
                clean = clean_prompt(txt)
                if clean:
                    yield {"kind": EV_PROMPT, "text": clean}
            elif item.get("type") == "output_text" and role == "assistant":
                yield {"kind": EV_ASSISTANT, "text": txt}

    def _tool_events(self, o, cwd):
        name = o.get("name") or ""
        yield {"kind": EV_TOOL, "name": name}
        if name not in ("Write", "Edit"):
            return
        args = o.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return
        if not isinstance(args, dict):
            return
        fp = args.get("file_path") or args.get("path") or args.get("target_file") or ""
        if not fp:
            return
        rel = rel_in_cwd(fp, cwd)
        if not rel:
            return
        if name == "Write":
            content = args.get("content")
            if isinstance(content, str) and content:
                yield {"kind": EV_WRITE, "rel": rel, "content": content}

    def _result_event(self, line):
        """只扫错误线索，不保留完整输出（体积控制）。"""
        if not ERROR_HINT.search(line[:1500]):
            return None
        try:
            o = json.loads(line)
        except Exception:
            return None
        txt = ""
        c = o.get("content")
        if isinstance(c, list):
            txt = " ".join(i.get("text", "") for i in c if isinstance(i, dict))
        elif isinstance(c, str):
            txt = c
        txt = (txt or "").strip().replace("\n", " ")
        return {"kind": EV_TOOL_RESULT, "text": txt[:220]} if txt else None

    # ---------- 回跳与补充信息
    def deep_link(self, sid: str) -> str:
        return f"workbuddy://chat/{sid}" if sid else ""

    def open_hint(self) -> str:
        return "在浏览器/资源管理器地址栏粘贴 `workbuddy://chat/<会话ID>`"

    def memory_dir(self, workspace):
        return Path(workspace) / ".workbuddy" / "memory"

    def workspace_root(self):
        return WORKSPACES_ROOT
