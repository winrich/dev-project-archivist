"""工作区外成果探测。

背景：会话的 cwd（工作区）与实际干活的目录经常不是同一个。实测某项目
工作区只有 .workbuddy，但 1078 次工具调用里有 259 次写在
D:/Projects/JoyBang/GitHubHostsAgent。归档器按设计只扫工作区，于是把这类
项目误判为「空壳」。

本模块从会话转录的中立事件流里提取「工作区外的写入」，按成果根目录归并，
让归档器/管理台能识别并标记这类项目。它只产出路径与计数，**不复制源码**，
不改变归档边界。

设计要点：
- 平台无关：消费 archiver.EV_WRITE 事件（外部写入 rel 为空、external 为绝对路径），
  不认识任何平台的原始格式。
- 归并键优先 git work-tree；无 git 时向上找工程入口文件（.git/.sln/.csproj/
  package.json/Cargo.toml 等）所在目录，避免同一仓库被拆成 Core/Properties 多条。
- 噪声过滤：AppData/Local/Temp、logs 等一次性目录不计入成果。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

# 这些父目录片段命中即视为临时/缓存/日志，不是成果目录
_NOISE_PARTS = (
    "/appdata/local/temp/", "/appdata/local/cache/",
    "/temp/", "/tmp/", "/.cache/", "/__pycache__/",
    "/node_modules/", "/.git/", "/logs/", "/log/",
    "/.workbuddy/", "/.claude/", "/.cursor/", "/.codex/",
)
# 偶发写入不算成果：单根至少这么多次写入才升级项目（避免 1 次配置写入误判）
MIN_WRITES_TO_UPGRADE = 5
# 强根标志（精确名）：看到就停，优先级高于单项目文件
_ROOT_STRONG = ("package.json", "pyproject.toml", "setup.py", "requirements.txt",
                "cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", ".git")
# 强根标志（按后缀）：.sln 在解决方案根，优先于内层 .csproj
_ROOT_STRONG_EXTS = (".sln", ".xcodeproj")
# 弱根标志：单项目文件，只有整条路径上都没有强标志时才采用
_ROOT_WEAK_EXTS = (".csproj", ".vcxproj", ".vbproj", ".fsproj")


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").rstrip("/")


def _under(path: str, root: str) -> bool:
    """path 是否等于 root 或位于其下（大小写不敏感，路径先经 _norm 规范化）。"""
    p, r = _norm(path).lower(), _norm(root).lower()
    return bool(r) and (p == r or p.startswith(r + "/"))


def is_noise(path: str) -> bool:
    """临时/缓存/日志路径返回 True（不纳入成果）。"""
    low = _norm(path).lower() + "/"
    return any(part in low for part in _NOISE_PARTS)


def _has_marker(d: str, names: tuple = (), exts: tuple = ()) -> bool:
    """目录是否含指定根标志。"""
    try:
        p = Path(d)
        if not p.is_dir():
            return False
        for name in names:
            if (p / name).exists():
                return True
        if exts:
            for child in p.iterdir():
                if child.is_file() and child.suffix.lower() in exts:
                    return True
    except OSError:
        return False
    return False


def find_project_root(abs_path: str) -> str:
    """把外部写入文件归并到成果根。

    优先级：git work-tree > 强标志(.sln/package.json/.git 等) > 弱标志(.csproj)。
    多层 C# 解决方案里 .sln 在解决方案根、.csproj 在内层项目目录——取 .sln 层
    才能在跨多个子项目时仍归并为一条。无标志时退化为文件所在目录。
    """
    p = _norm(abs_path)
    d = os.path.dirname(p) or p
    try:
        import archiver as A
        wt = A.git_work_tree(d)
        if wt:
            return _norm(wt)
    except Exception:
        pass
    cur, weak = d, ""
    while cur and cur.count("/") > 1:
        if _has_marker(cur, _ROOT_STRONG, _ROOT_STRONG_EXTS):
            return cur
        if not weak and _has_marker(cur, (), _ROOT_WEAK_EXTS):
            weak = cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if weak:
        return weak
    # 没有工程标志的目录不算「源码成果」（如家目录、.workbuddy 配置目录）——
    # 宁可漏记偶发写入，也不要把配置读写误判成工程成果。
    return ""


def collect_external(events, workspace: str, archive_root: str = "") -> dict:
    """消费中立事件流，返回 {root: {writes, files:set, first, last}}。

    只统计工作区外写入（EV_WRITE 且 rel 为空、external 非空）。
    archive_root 为归档目标目录：归档器自己写出的归档文件不算「外部成果」，
    否则归档任务会话会把归档输出误判成外部工程（实测一次归档 51 条写入全在此）。
    """
    import archiver as A
    ws = _norm(workspace).lower()
    out = defaultdict(lambda: {"writes": 0, "files": set(), "first": None, "last": None})
    cur_ts = None
    for ev in events:
        kind = ev.get("kind")
        if kind == A.EV_TS:
            cur_ts = ev.get("value") or cur_ts
            continue
        if kind != A.EV_WRITE:
            continue
        if ev.get("rel") or not ev.get("external"):
            continue
        raw = _norm(ev["external"])
        if not raw or is_noise(raw):
            continue
        # 归档器自己的输出目录不算外部成果（归档任务会话的高频误判源）
        if archive_root and _under(raw, archive_root):
            continue
        root = find_project_root(raw)
        if not root or is_noise(root):
            continue
        # 写回自己工作区的不算外部成果
        if root.lower() == ws or root.lower().startswith(ws + "/"):
            continue
        rec = out[root]
        rec["writes"] += 1
        rel = raw[len(root):].lstrip("/") if raw.lower().startswith(root.lower()) else raw
        if rel:
            rec["files"].add(rel)
        if cur_ts:
            rec["first"] = cur_ts if rec["first"] is None else min(rec["first"], cur_ts)
            rec["last"] = cur_ts if rec["last"] is None else max(rec["last"], cur_ts)
    return dict(out)


def detect_for_project(sessions: list, idx, adapters: list, by_name: dict,
                       workspace: str, archive_root: str = "") -> list:
    """扫描一个项目的所有会话转录，返回外部成果清单（写入次数降序）。

    sessions: [{sid, source, ...}]
    idx: MultiIndex，提供 transcript(sid)
    adapters/by_name: 平台适配器，用于 iter_events
    archive_root: 归档目标目录，其内的写入是归档器输出，不计入成果。
    """
    merged = defaultdict(lambda: {"writes": 0, "files": set(), "first": None, "last": None})
    for s in sessions:
        tp = idx.transcript(s.get("sid") or "")
        if not tp:
            continue
        ad = by_name.get(s.get("source") or "") or (adapters[0] if adapters else None)
        if ad is None:
            continue
        try:
            events = ad.iter_events(tp, workspace)
            part = collect_external(events, workspace, archive_root=archive_root)
        except Exception:
            continue
        for root, rec in part.items():
            m = merged[root]
            m["writes"] += rec["writes"]
            m["files"] |= rec["files"]
            for k in ("first", "last"):
                v = rec.get(k)
                if v:
                    m[k] = v if m[k] is None else (min(m[k], v) if k == "first" else max(m[k], v))
    out = []
    for root, rec in merged.items():
        if rec["writes"] < MIN_WRITES_TO_UPGRADE:
            continue
        out.append({
            "root": root,
            "writes": rec["writes"],
            "files": len(rec["files"]),
            "first": rec["first"],
            "last": rec["last"],
        })
    out.sort(key=lambda x: (-x["writes"], x["root"]))
    return out
