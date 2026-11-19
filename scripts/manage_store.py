"""管理功能数据层：人工配置读写、改名事务、关注/置顶/删除。

设计约束：
- 人工配置以「工作区路径」为键写进 overrides.json（不是目录名），改名不失联。
- 所有写操作由调用方持有 Lock(target)；本模块内部只做原子写。
- 改名是唯一有破坏面的操作：先备份 → 改目录 → 改 state → 重建索引；任一步失败整体回滚。
- 不引入第三方依赖，复用 archiver.py 已有的工具函数。
"""
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import archiver as A

# 人工字段（写入 overrides.json 的单个条目）
MANUAL_FIELDS = ("title", "purpose", "type", "pinned", "watching", "note", "skip")
BOOL_FIELDS = ("pinned", "watching", "skip")
TYPE_CHOICES = ("代码项目", "文档任务", "混合产物")
BACKUP_DIR = "_trash/_manage_backups"


def norm_key(key: str) -> str:
    """把工作区路径规范化为 state.json / overrides.json 共用的键"""
    return (key or "").replace("\\", "/").rstrip("/").lower()


class ManageError(Exception):
    """面向界面的可展示错误"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _load_json(p: Path, default):
    try:
        return json.loads(A.read_text(p) or "") or default
    except Exception:
        return default


def backup_state(target: Path) -> Path:
    """把 state.json / overrides.json / index.json 存一份到 _trash/_manage_backups/"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = target / BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for name in (A.STATE_FILE, "overrides.json", A.INDEX_JSON, A.INDEX_MD):
        src = target / name
        if src.exists():
            shutil.copy2(str(src), str(dest / name))
    return dest


def restore_backup(target: Path, backup: Path) -> bool:
    """从备份恢复上述四个文件（改名事务失败时调用）"""
    if not backup or not backup.exists():
        return False
    ok = True
    for name in (A.STATE_FILE, "overrides.json", A.INDEX_JSON, A.INDEX_MD):
        src = backup / name
        if src.exists():
            try:
                shutil.copy2(str(src), str(target / name))
            except Exception:
                ok = False
    return ok


class ManageStore:
    def __init__(self, target: Path):
        self.target = Path(target)
        self.state = A.State(self.target)
        self.overrides = A.load_overrides(self.target)
        self.raw_overrides = _load_json(self.target / "overrides.json", {})

    # ------------------------------------------------------------ 读
    def projects(self) -> list:
        """聚合 state + 人工字段，返回管理列表所需的全部条目"""
        idx = _load_json(self.target / A.INDEX_JSON, {}).get("projects") or []
        purposes = {norm_key(p.get("workspace")): p.get("purpose", "") for p in idx}
        out = []
        for key, st in self.state.data["projects"].items():
            ov = self.overrides.get(norm_key(key)) or self.overrides.get(
                (st.get("dir") or "").lower(), {})
            name = ov.get("title") or self._summary_title(st) or st.get("name") or ""
            out.append({
                "key": key,
                "dir": st.get("dir") or "",
                "name": name,
                "workspace": st.get("workspace") or "",
                "type": st.get("type") or "",
                "language": st.get("language") or "",
                "first_dev": A.day(st.get("first_ts")),
                "last_dev": A.day(st.get("last_ts")),
                "last_archived": A.day(st.get("last_archived")),
                "sessions": st.get("sessions", 0),
                "files": st.get("files", 0),
                "bytes": st.get("bytes", 0),
                "status": st.get("status") or "",
                "archived": bool(st.get("archived")),
                "pinned": bool(ov.get("pinned")),
                "watching": ov.get("watching") is not False,
                "skip": bool(ov.get("skip")),
                "note": ov.get("note") or "",
                "purpose": ov.get("purpose") or purposes.get(norm_key(key), ""),
                "manual": bool(ov.get("title")) or st.get("name_source") == "manual",
                "updated_at": ov.get("updated_at") or "",
            })
        # 置顶优先，其次按最后开发时间新→旧
        out.sort(key=lambda x: (not x["pinned"], x["last_dev"]), reverse=False)
        out.sort(key=lambda x: x["pinned"], reverse=True)
        return out

    def _summary_title(self, st: dict) -> str:
        adir = self.target / (st.get("dir") or "")
        try:
            return (A.load_summary(adir) or {}).get("title") or ""
        except Exception:
            return ""

    def detail(self, key: str) -> dict:
        """项目详情：聚合状态、人工配置、摘要、会话原文、证据与产物清单"""
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        adir = self.target / (st.get("dir") or "")
        ov = self.overrides.get(norm_key(key)) or {}
        s = A.load_summary(adir) if adir.exists() else {}
        d = self.projects()
        item = next((x for x in d if x["key"] == key), {})
        conv = A.parse_conversations(adir / A.META_DIR / A.CONVERSATIONS_FILE) if adir.exists() else []
        files = [str(Path(p).as_posix()) for p in A.walk_source(adir)][:200] if adir.exists() else []
        links = []
        for c in conv[:12]:
            links.append({"title": c.get("title") or "", "time": c.get("time") or "",
                          "link": c.get("link") or ""})
        return {
            **item,
            "summary": s.get("summary") or s.get("purpose") or "",
            "tech": s.get("tech") or s.get("technology") or "",
            "language_full": s.get("language") or st.get("language") or "",
            "renamed_from": st.get("renamed_from") or "",
            "git_repo": (adir / ".git").exists(),
            "readme": str(adir / "README.md") if (adir / "README.md").exists() else "",
            "conversations": links,
            "source_files": files,
        }

    # ------------------------------------------------------------ 写
    def _ov_entry(self, key: str) -> dict:
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        nk = norm_key(key) or norm_key(st.get("workspace")) or norm_key(st.get("dir"))
        if not nk:
            raise ManageError("项目缺少有效标识")
        ent = self.raw_overrides.get(nk)
        if not isinstance(ent, dict):
            ent = {}
            self.raw_overrides[nk] = ent
        self.overrides[nk] = ent
        return ent

    def _save_overrides(self):
        A.atomic_write(self.target / "overrides.json",
                       json.dumps(self.raw_overrides, ensure_ascii=False, indent=2) + "\n")

    def _rebuild_index(self, state=None):
        """用当前 state 重建 INDEX.md / index.json，使改名、隐藏标记立即生效"""
        state = state or self.state
        rows = []
        for k, st in state.data["projects"].items():
            if not st.get("archived") or not (self.target / (st.get("dir") or "")).exists():
                continue
            rows.append((st, self.target / st["dir"]))
        ov = {"_" + k: v for k, v in self.overrides.items()}  # 原样传，忽略下划线键
        ov = {k: v for k, v in ov.items() if not k.startswith("_")}
        A.write_index(self.target, rows, state, ov)
        state.save()

    def _audit(self, action: str, detail: str):
        A.append_changelog(self.target, action, detail)

    def set_fields(self, key: str, fields: dict) -> dict:
        """通用字段更新。只接受 MANUAL_FIELDS 白名单。"""
        unknown = [k for k in fields if k not in MANUAL_FIELDS]
        if unknown:
            raise ManageError("不支持的字段：" + "、".join(unknown))
        ent = self._ov_entry(key)
        applied = {}
        for k, v in fields.items():
            if k in BOOL_FIELDS:
                if not isinstance(v, bool):
                    raise ManageError(f"{k} 必须是布尔值")
                v = bool(v)
            elif k == "type":
                if v not in TYPE_CHOICES:
                    raise ManageError("类型必须是：" + " / ".join(TYPE_CHOICES))
            elif k == "title":
                if not isinstance(v, str) or not v.strip():
                    raise ManageError("项目名不能为空")
                v = A.safe_name(v.strip())
            elif k in ("purpose", "note"):
                if not isinstance(v, str):
                    raise ManageError(f"{k} 必须是文本")
                v = v.strip()[:2000]
            ent[k] = v
            applied[k] = v
        if applied:
            ent["updated_at"] = _now()
            self._save_overrides()
            self._rebuild_index()
            self._audit("管理", f"{key}：{', '.join(f'{k}={v}' for k, v in applied.items())}")
        return self.detail(key)

    def set_watching(self, key: str, watching: bool) -> dict:
        return self.set_fields(key, {"watching": bool(watching)})

    def set_pinned(self, key: str, pinned: bool) -> dict:
        return self.set_fields(key, {"pinned": bool(pinned)})

    def rename(self, key: str, new_title: str) -> dict:
        """改显示名并同步重命名归档目录；带备份与整体回滚"""
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        new_name = A.safe_name(new_title or "")
        if not new_name or new_name in ("未命名项目",):
            raise ManageError("项目名不能为空或无效")
        old_dir = st.get("dir") or ""
        old_adir = self.target / old_dir
        if not old_adir.exists():
            raise ManageError("归档目录不存在，可能已被清理")
        prefix = A.day(st.get("first_ts")) or "0000-00-00"
        base = f"{prefix}_{new_name}"
        cand, n = base, 2
        while (self.target / cand).exists() and (self.target / cand) != old_adir:
            cand = f"{base}-{n}"
            n += 1
        new_adir = self.target / cand

        backup = backup_state(self.target)
        old_state = json.loads(json.dumps(st))  # 深拷贝
        old_ent = dict(self._ov_entry(key))
        try:
            os.rename(str(old_adir), str(new_adir))
            st["dir"] = cand
            st["name"] = new_name
            st["renamed_from"] = old_dir
            st["name_source"] = "manual"
            ent = self._ov_entry(key)
            ent["title"] = new_name
            ent["updated_at"] = _now()
            self._save_overrides()
            self._rebuild_index()
            self._audit("改名", f"{old_dir} → {cand}（人工）")
        except Exception as e:
            # 整体回滚：目录、state、overrides、index 一起恢复
            if new_adir.exists() and not old_adir.exists():
                try:
                    os.rename(str(new_adir), str(old_adir))
                except Exception:
                    pass
            restore_backup(self.target, backup)
            self.state = A.State(self.target)
            self.overrides = A.load_overrides(self.target)
            self.raw_overrides = _load_json(self.target / "overrides.json", {})
            raise ManageError(f"改名失败，已回滚：{e}")
        return self.detail(key)

    def delete(self, key: str, purge: bool = False) -> dict:
        """停止归档并把目录移入 _trash/（软删除）。purge=True 需调用方二次确认。"""
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        dirname = st.get("dir") or ""
        ent = self._ov_entry(key)
        ent["skip"] = True
        ent["watching"] = False
        ent["updated_at"] = _now()
        self._save_overrides()
        if dirname and (self.target / dirname).exists():
            A.trash_project(self.target, dirname, "管理界面删除")
        st["archived"] = False
        if purge:
            # 彻底删除 _trash 中本次产生的副本：只删这一份，不碰其它项目
            # 由调用方保证已二次确认；这里保留最后一道路径校验。
            trash = self.target / "_trash"
            for d in trash.glob(dirname + "__*"):
                if d.is_dir() and d.parent == trash:
                    shutil.rmtree(str(d), onerror=_force_rm)
        self._rebuild_index()
        self._audit("删除", f"{dirname}（{'彻底' if purge else '软删除'}，停止归档）")
        return {"key": key, "deleted": True, "purge": purge}


def _force_rm(func, path, exc):
    """Windows git 只读对象的兜底删除"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except Exception:
        pass
