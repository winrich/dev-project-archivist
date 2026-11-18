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
                   EV_TOOL, EV_TOOL_RESULT, EV_TS, EV_WRITE, Index, TOOL_SHELL,
                   extract_cmd, iso_to_ms, rel_in_cwd, safe_lines, tool_type,
                   usage_event)
from .base import clean_prompt as _clean_prompt

HOME = Path.home()
WB_HOME = HOME / ".workbuddy"
PROJECTS_DIR = WB_HOME / "projects"
SESSION_DB = WB_HOME / "workbuddy.db"
ARTIFACT_INDEX = WB_HOME / "artifact-index"      # 预留：产物索引，当前版本未使用
FILE_HISTORY = WB_HOME / "file-history"          # 预留：文件版本快照，当前版本未使用
WORKSPACES_ROOT = HOME / "WorkBuddy"

# 会改动文件的工具：Write 带正文，其余只记录路径
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# 会话正文里注入的上下文块，需要剥离后才是用户真实提问
_INJECT_BLOCK = re.compile(
    r"<(?:current_time|user_info|identity_context|product_identity|"
    r"tone_and_style|memory|user_memory|project_context|connector-status|"
    r"craft_mode|project_layout|additional-data)[^>]*>")


def clean_prompt(txt: str) -> str:
    """剥离 WorkBuddy 特有的身份注入块后，交回通用清洗。"""
    return _clean_prompt(txt, _INJECT_BLOCK)


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
            sid = p.stem
            self.jsonl[sid] = p
            if sid in self.meta:
                continue
            # 数据库里没有这一行（历史会话/已删除）时，从转录头部补出 cwd 与起始时间。
            # 不补的话这些会话因缺 cwd 被整体丢弃，按天聚合会凭空少掉几天。
            info = self._head_info(p)
            if info:
                self.meta[sid] = info

    def _head_info(self, path: Path, max_lines: int = 60):
        """只读转录头部，取 cwd 与首个时间戳；updated 用文件 mtime 兜底。"""
        cwd, first_ts = "", None
        try:
            for i, line in enumerate(safe_lines(path)):
                if i >= max_lines:
                    break
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not cwd and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                ts = o.get("timestamp")
                if ts and first_ts is None:
                    first_ts = int(ts) if not isinstance(ts, str) else None
                if cwd and first_ts:
                    break
        except Exception:
            return None
        if not cwd:
            return None
        try:
            mtime = int(path.stat().st_mtime * 1000)
        except Exception:
            mtime = first_ts
        return {
            "cwd": cwd,
            "title": "",
            "created": first_ts or mtime,
            "updated": mtime,
            "model": "",
            "playground": False,
            "automation": False,
            "source": "workbuddy",
            "meta_from": "transcript",   # 标记来源，便于排错
        }

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
            for line in safe_lines(path):
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
                ts = iso_to_ms(o.get("timestamp"))
                if ts:
                    yield {"kind": EV_TS, "value": ts}
                # token 用量挂在每次模型调用上（function_call / message 两种记录都有），
                # 这是把「成本」和「产出」连起来的唯一字段，必须收
                u = usage_event((o.get("message") or {}).get("usage")
                                if isinstance(o.get("message"), dict) else None)
                if u:
                    yield u
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
        args = o.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = None
        if not isinstance(args, dict):
            args = {}
        ev = {"kind": EV_TOOL, "name": name}
        # shell 类工具带上首行命令：日志里「跑了 8 次构建」比「调了 156 次工具」有用
        if tool_type(name) == TOOL_SHELL:
            cmd = extract_cmd(args)
            if cmd:
                ev["cmd"] = cmd
        yield ev
        if name not in EDIT_TOOLS:
            return
        # 同一批数据里 _file_path 与 filePath 两种写法并存（实测约 6% 走驼峰），
        # 只认其中一种会静默丢掉这些写入，源码与日志都会缺。
        fp = (args.get("file_path") or args.get("filePath")
              or args.get("path") or args.get("target_file")
              or args.get("notebook_path") or args.get("notebookPath") or "")
        if not fp:
            return
        op = "write" if name == "Write" else "edit"
        body = args.get("content") if name == "Write" else ""
        body = body if isinstance(body, str) else ""
        rel = rel_in_cwd(fp, cwd)
        if not rel:
            # 落在工作区外的写入：归档侧按设计丢弃，但按天聚合要知道「还有多少在别处」
            yield {"kind": EV_WRITE, "rel": "", "external": str(fp), "op": op}
            return
        yield {"kind": EV_WRITE, "rel": rel, "op": op, "content": body}

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
