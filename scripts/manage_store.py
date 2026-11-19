"""管理功能数据层：人工配置读写、改名事务、关注/置顶/删除。

设计约束：
- 人工配置以「工作区路径」为键写进 overrides.json（不是目录名），改名不失联。
- 所有写操作由调用方持有 Lock(target)；本模块内部只做原子写。
- 改名是唯一有破坏面的操作：先备份 → 改目录 → 改 state → 重建索引；任一步失败整体回滚。
- 不引入第三方依赖，复用 archiver.py 已有的工具函数。
"""
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import archiver as A

# 活跃度算法参数（集中在此，便于调权与单测）
ACT_RECENCY_HALFLIFE = 30      # 近期性半衰期（天）：30 天前得分减半
ACT_FREQ_SCALE = 25.0          # 频次系数：25*log2(1+sessions)
ACT_OUTPUT_SCALE = 12.0        # 产出系数：12*log2(1+files)
ACT_CONSISTENCY_FULL = 10      # 持续度满分所需的不同活跃天数
ACT_WEIGHTS = {"recency": 0.4, "freq": 0.3, "output": 0.2, "consistency": 0.1}
ACT_SPARK_DAYS = 14            # 迷你图窗口天数
ACT_LEVELS = ((70, "hot"), (45, "active"), (20, "calm"), (0, "idle"))

# 人工字段（写入 overrides.json 的单个条目）
MANUAL_FIELDS = ("title", "purpose", "type", "pinned", "watching", "note", "skip")
BOOL_FIELDS = ("pinned", "watching", "skip")
TYPE_CHOICES = ("代码项目", "文档任务", "混合产物")
BACKUP_DIR = "_trash/_manage_backups"
REPORT_INDEX = "report_index.json"      # 报告档案索引（archiver.cmd_report 负责登记）
REPORT_SUBDIR = "report"                # 归档 HTML 存放目录


def norm_key(key: str) -> str:
    """把工作区路径规范化为 state.json / overrides.json 共用的键"""
    return (key or "").replace("\\", "/").rstrip("/").lower()


# ---------------------------------------------------------------- 活跃度算法

def conv_day_counts(adir: Path) -> dict:
    """从 _meta/conversations.md 聚合「每日会话数」与活跃日期集合。

    conversations.md 的「时间」字段来自 A.iso()，形如 '2026-11-18 09:30'；
    取前 10 位即 YYYY-MM-DD。文件缺失/损坏时返回空 dict（降级：持续度=0、迷你图全零）。
    """
    try:
        conv = A.parse_conversations(adir / A.META_DIR / A.CONVERSATIONS_FILE)
    except Exception:
        return {}
    counts = {}
    for c in conv:
        d = (c.get("time") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            counts[d] = counts.get(d, 0) + 1
    return counts


def activity_of(st: dict, adir: Path, today: datetime = None) -> dict:
    """计算单个项目的活跃度（0-100）与近 N 天迷你图数据。

    四维加权（近期性 40% / 频次 30% / 产出 20% / 持续度 10%）：
    - 近期性 R = 100 * 0.5^(距今天数 / 半衰期)，越近越高；
    - 频次   F = min(100, 25*log2(1+会话数))，对数压缩避免头部碾压；
    - 产出   O = min(100, 12*log2(1+文件数))；
    - 持续度 C = min(100, 活跃天数 / 10 * 100)，来自会话明细的不同日期数。

    today 可注入以便单测固定「今天」。返回分项分数便于前端解释与调参。
    """
    today = (today or datetime.now()).date()
    sessions = int(st.get("sessions") or 0)
    files = int(st.get("files") or 0)
    last_ts = st.get("last_ts")
    last_day = A.day(last_ts) if last_ts else ""

    # 近期性：距今天数（last_ts 为毫秒）
    try:
        age = max(0, (today - datetime.strptime(last_day, "%Y-%m-%d").date()).days) \
            if last_day else 9999
    except Exception:
        age = 9999
    recency = 100.0 * (0.5 ** (age / float(ACT_RECENCY_HALFLIFE)))
    freq = min(100.0, ACT_FREQ_SCALE * math.log2(1 + sessions))
    output = min(100.0, ACT_OUTPUT_SCALE * math.log2(1 + files))

    counts = conv_day_counts(adir) if adir else {}
    active_days = len(counts)
    consistency = min(100.0, active_days / float(ACT_CONSISTENCY_FULL) * 100.0)

    score = int(round(ACT_WEIGHTS["recency"] * recency
                      + ACT_WEIGHTS["freq"] * freq
                      + ACT_WEIGHTS["output"] * output
                      + ACT_WEIGHTS["consistency"] * consistency))
    score = max(0, min(100, score))
    level = next(name for threshold, name in ACT_LEVELS if score >= threshold)

    spark = []
    for i in range(ACT_SPARK_DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        spark.append({"date": d, "count": counts.get(d, 0)})

    return {
        "score": score,
        "level": level,
        "active_days": active_days,
        "age_days": age if age < 9999 else None,
        "parts": {
            "recency": round(recency, 1), "freq": round(freq, 1),
            "output": round(output, 1), "consistency": round(consistency, 1),
        },
        "spark": spark,
    }


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
            adir = self.target / (st.get("dir") or "")
            out.append({
                "activity": activity_of(st, adir) if adir.exists() else None,
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

    def overview(self) -> dict:
        """数据概览：整体情况，全部来自 state/overrides，不触发会话解析以外的重活。"""
        items = self.projects()
        watching = [p for p in items if p["watching"] and not p["skip"]]
        by_type, by_lang, by_level = {}, {}, {}
        total_files = total_bytes = 0
        for p in items:
            by_type[p["type"] or "未分类"] = by_type.get(p["type"] or "未分类", 0) + 1
            lang = p["language"] or "未标注"
            by_lang[lang] = by_lang.get(lang, 0) + 1
            act = p.get("activity")
            if act:
                by_level[act["level"]] = by_level.get(act["level"], 0) + 1
            total_files += int(p["files"] or 0)
            total_bytes += int(p["bytes"] or 0)

        # 近 14 天全局活动量：把各项目迷你图按天相加
        spark_days = ACT_SPARK_DAYS
        spark = [0] * spark_days
        for p in items:
            act = p.get("activity")
            if not act:
                continue
            for i, pt in enumerate(act.get("spark") or []):
                if i < spark_days:
                    spark[i] += int(pt.get("count") or 0)

        # 最近更新的关注中项目（Top 8）
        recent = sorted(watching, key=lambda p: p["last_dev"] or "", reverse=True)[:8]
        recent = [{"name": p["name"], "last_dev": p["last_dev"],
                   "score": (p.get("activity") or {}).get("score")} for p in recent]

        days = [p["first_dev"] for p in items if p.get("first_dev")]
        return {
            "total": len(items),
            "watching": len(watching),
            "archived": sum(1 for p in items if p["archived"]),
            "total_files": total_files,
            "total_bytes": total_bytes,
            "by_type": by_type,
            "by_lang": dict(sorted(by_lang.items(), key=lambda kv: -kv[1])),
            "by_level": by_level,
            "spark": spark,
            "recent": recent,
            "span": {"from": min(days) if days else "", "to": max(days) if days else ""},
        }

    def ranking(self, limit: int = 20) -> list:
        """活跃度排行：按分数降序，忽略无 activity 的项。"""
        items = [p for p in self.projects()
                 if p.get("activity") and p["watching"] and not p["skip"]]
        items.sort(key=lambda p: p["last_dev"] or "", reverse=True)  # 同分：近的在前
        items.sort(key=lambda p: p["activity"]["score"], reverse=True)  # 主排序：分数降序
        return [{"name": p["name"], "dir": p["dir"], "key": p["key"],
                 "score": p["activity"]["score"], "level": p["activity"]["level"],
                 "sessions": p["sessions"], "last_dev": p["last_dev"],
                 "spark": p["activity"]["spark"]} for p in items[:limit]]

    # ------------------------------------------------------------ 报告档案

    def report_list(self, offset: int = 0, limit: int = 0) -> dict:
        """档案列表：索引里按 to/generated_at 倒序（索引写入时已排好）。

        limit=0 表示不截断。返回 {total, items}，供管理台分页。
        """
        idx = A.load_report_index(self.target)
        reports = idx.get("reports", [])
        total = len(reports)
        if limit and limit > 0:
            items = reports[offset:offset + limit]
        else:
            items = reports[offset:] if offset else reports
        return {"total": total, "items": items, "updated_at": idx.get("updated_at", "")}

    def report_archive(self, date_str: str) -> list:
        """按日期检索覆盖该日的报告：from <= date <= to。

        date_str 必须是 YYYY-MM-DD，否则返回空列表。
        """
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str or ""):
            return []
        idx = A.load_report_index(self.target)
        out = []
        for r in idx.get("reports", []):
            f, t = r.get("from", ""), r.get("to", "")
            if f and t and f <= date_str <= t:
                item = dict(r)
                # 该日在这份报告区间内是第几天（便于前端展示"第 X/N 天"）
                try:
                    from datetime import date as _date
                    d0 = _date.fromisoformat(f)
                    d1 = _date.fromisoformat(date_str)
                    item["day_index"] = (d1 - d0).days + 1
                except Exception:
                    item["day_index"] = 0
                out.append(item)
        out.sort(key=lambda r: (r.get("to", ""), r.get("generated_at", "")), reverse=True)
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
            if "title" in applied:
                st = self.state.data["projects"].get(key)
                if st:
                    st["name"] = applied["title"]
                    st["name_source"] = "manual"
            self._rebuild_index()
            self._audit("管理", f"{key}：{', '.join(f'{k}={v}' for k, v in applied.items())}")
        return self.detail(key)

    def set_watching(self, key: str, watching: bool) -> dict:
        return self.set_fields(key, {"watching": bool(watching)})

    def set_pinned(self, key: str, pinned: bool) -> dict:
        return self.set_fields(key, {"pinned": bool(pinned)})

    def rename(self, key: str, new_title: str) -> dict:
        """只改显示名（override.title），目录名保持不变。

        下一次 render 会以 override.title 渲染 README/索引，因此立即生效；
        name_source=manual 同时阻止 LLM 摘要把显示名改回去。
        """
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        new_name = A.safe_name(new_title or "")
        if not new_name or new_name in ("未命名项目",):
            raise ManageError("项目名不能为空或无效")
        old_name = st.get("name") or ""
        ent = self._ov_entry(key)
        backup = backup_state(self.target)
        try:
            ent["title"] = new_name
            ent["updated_at"] = _now()
            self._save_overrides()
            st["name"] = new_name
            st["name_source"] = "manual"
            self._rebuild_index()
            self._audit("改名", f"{old_name} → {new_name}（仅显示名，目录不变）")
        except Exception as e:
            restore_backup(self.target, backup)
            self.state = A.State(self.target)
            self.overrides = A.load_overrides(self.target)
            self.raw_overrides = _load_json(self.target / "overrides.json", {})
            raise ManageError(f"改名失败，已回滚：{e}")
        return self.detail(key)

    def rename_dir(self, key: str, new_dirname: str) -> dict:
        """独立入口：只改归档目录名（显示名不受影响），带备份与整体回滚。"""
        st = self.state.data["projects"].get(key)
        if not st:
            raise ManageError("项目不存在或已被清理")
        raw = (new_dirname or "").strip()
        if not raw or raw in (".", ".."):
            raise ManageError("目录名不能为空或无效")
        if re.search(r'[\\/:*?"<>|\r\n\t]', raw):
            raise ManageError("目录名含非法字符（\\ / : * ? \" < > |）")
        cand = raw[:60].strip(" .")
        if not cand:
            raise ManageError("目录名不能只有空格或点")
        old_dir = st.get("dir") or ""
        old_adir = self.target / old_dir
        if not old_adir.exists():
            raise ManageError("归档目录不存在，可能已被清理")
        new_adir = self.target / cand
        if new_adir.exists() and new_adir != old_adir:
            raise ManageError("目标目录已存在，请换一个名字")
        if cand == old_dir:
            return self.detail(key)

        backup = backup_state(self.target)
        try:
            os.rename(str(old_adir), str(new_adir))
            st["dir"] = cand
            st["renamed_from"] = old_dir
            self._save_overrides()  # 键按 workspace，不含目录名，无需改写
            self._rebuild_index()
            self._audit("目录改名", f"{old_dir} → {cand}（显示名不变）")
        except Exception as e:
            if new_adir.exists() and not old_adir.exists():
                try:
                    os.rename(str(new_adir), str(old_adir))
                except Exception:
                    pass
            restore_backup(self.target, backup)
            self.state = A.State(self.target)
            self.overrides = A.load_overrides(self.target)
            self.raw_overrides = _load_json(self.target / "overrides.json", {})
            raise ManageError(f"目录改名失败，已回滚：{e}")
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
