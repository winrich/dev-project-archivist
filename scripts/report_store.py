"""报告档案与会话明细的 SQLite 存储层（Python 标准库 sqlite3，零第三方依赖）。

设计约束：
- 短连接：每次操作开→关，配合 WAL 模式，支持管理台与夜间任务并发读写。
- 写事务用 BEGIN IMMEDIATE，避免两个写者同时进入。
- 主键即业务键：reports.name / sessions.sid / meta.key，全部 UPSERT 幂等。
- 只存元信息，HTML 正文与转录仍留在文件系统——DB 坏了不毁原始资料。
- 旧 report_index.json 保留作回退源，不因迁移而删除。
"""
import json
import sqlite3
from pathlib import Path

DB_NAME = "reports.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    name         TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT '',
    date_from    TEXT NOT NULL DEFAULT '',
    date_to      TEXT NOT NULL DEFAULT '',
    days         INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL DEFAULT '',
    prompts      INTEGER NOT NULL DEFAULT 0,
    sessions     INTEGER NOT NULL DEFAULT 0,
    projects     INTEGER NOT NULL DEFAULT 0,
    files        INTEGER NOT NULL DEFAULT 0,
    tokens       INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    sid       TEXT PRIMARY KEY,
    source    TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL DEFAULT '',
    title     TEXT NOT NULL DEFAULT '',
    created   INTEGER NOT NULL DEFAULT 0,
    updated   INTEGER NOT NULL DEFAULT 0,
    status    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reports_to     ON reports(date_to DESC);
CREATE INDEX IF NOT EXISTS idx_reports_source ON reports(source, date_to DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_src   ON sessions(source, created DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_ws    ON sessions(workspace);
"""

REPORT_FIELDS = ("name", "source", "date_from", "date_to", "days", "generated_at",
                 "prompts", "sessions", "projects", "files", "tokens", "sha256", "bytes")


def db_path(target: Path) -> Path:
    return Path(target) / DB_NAME


def _connect(target: Path) -> sqlite3.Connection:
    p = db_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # 读写并发
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(target: Path) -> bool:
    """幂等建表并写入 schema 版本。返回是否新建了库文件。"""
    p = db_path(target)
    is_new = not p.exists()
    conn = _connect(target)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()
    return is_new


class ReportDB:
    def __init__(self, target: Path):
        self.target = Path(target)

    # ------------------------------------------------------------ 报告档案
    def upsert_report(self, entry: dict) -> None:
        """登记/覆盖一条报告档案。调用方可在外层持 A.Lock(target)。"""
        row = {
            "name": str(entry.get("name") or ""),
            "source": str(entry.get("source") or ""),
            "date_from": str(entry.get("from") or ""),
            "date_to": str(entry.get("to") or ""),
            "days": int(entry.get("days") or 0),
            "generated_at": str(entry.get("generated_at") or ""),
            "prompts": int(entry.get("prompts") or 0),
            "sessions": int(entry.get("sessions") or 0),
            "projects": int(entry.get("projects") or 0),
            "files": int(entry.get("files") or 0),
            "tokens": int(entry.get("tokens") or 0),
            "sha256": str(entry.get("sha256") or ""),
            "bytes": int(entry.get("bytes") or 0),
        }
        if not row["name"]:
            return
        conn = _connect(self.target)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cols = ", ".join(REPORT_FIELDS)
            ph = ", ".join("?" for _ in REPORT_FIELDS)
            upd = ", ".join(f"{c}=excluded.{c}" for c in REPORT_FIELDS if c != "name")
            conn.execute(f"INSERT INTO reports({cols}) VALUES({ph}) "
                         f"ON CONFLICT(name) DO UPDATE SET {upd}",
                         tuple(row[c] for c in REPORT_FIELDS))
            conn.commit()
        finally:
            conn.close()

    def list_reports(self, offset: int = 0, limit: int = 0,
                     source: str = "") -> dict:
        """档案列表，按 date_to/generated_at 倒序。limit=0 不截断。"""
        conn = _connect(self.target)
        try:
            where, args = "", []
            if source:
                where = " WHERE source = ?"
                args.append(source)
            total = conn.execute(f"SELECT COUNT(*) FROM reports{where}", args).fetchone()[0]
            sql = ("SELECT name, source, date_from AS 'from', date_to AS 'to', days, "
                   "generated_at, prompts, sessions, projects, files, tokens, sha256, bytes "
                   f"FROM reports{where} ORDER BY date_to DESC, generated_at DESC")
            if limit and limit > 0:
                sql += " LIMIT ? OFFSET ?"
                args += [limit, max(0, offset)]
            elif offset:
                sql += " LIMIT -1 OFFSET ?"
                args.append(max(0, offset))
            items = [dict(r) for r in conn.execute(sql, args).fetchall()]
            meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
            return {"total": total, "items": items,
                    "updated_at": meta.get("updated_at", "")}
        finally:
            conn.close()

    def archive_by_date(self, date_str: str) -> list:
        """覆盖给定日期的报告：date_from <= date <= date_to，倒序。"""
        conn = _connect(self.target)
        try:
            rows = conn.execute(
                "SELECT name, source, date_from AS 'from', date_to AS 'to', days, "
                "generated_at, prompts, sessions, projects, files, tokens, sha256, bytes "
                "FROM reports WHERE date_from <= ? AND date_to >= ? "
                "ORDER BY date_to DESC, generated_at DESC",
                (date_str, date_str)).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    from datetime import date as _date
                    item["day_index"] = (_date.fromisoformat(date_str)
                                         - _date.fromisoformat(item["from"])).days + 1
                except Exception:
                    item["day_index"] = 0
                out.append(item)
            return out
        finally:
            conn.close()

    def count_reports(self) -> int:
        conn = _connect(self.target)
        try:
            return conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        finally:
            conn.close()

    def touch_updated(self) -> None:
        from datetime import datetime
        conn = _connect(self.target)
        try:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('updated_at',?)",
                         (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 会话明细
    def replace_sessions(self, index) -> int:
        """从 MultiIndex 全量替换 sessions 表，返回写入条数。

        全量替换而非增量：索引本身是每次从头构建的，增量对账反而更容易出错。
        万级记录在事务内一次完成，耗时可接受。
        """
        rows = []
        for sid in index.ids():
            info = index.info(sid) or {}
            rows.append((str(sid), str(info.get("source") or ""),
                         str(info.get("cwd") or ""), str(info.get("title") or ""),
                         int(info.get("created") or 0), int(info.get("updated") or 0),
                         str(info.get("status") or "")))
        conn = _connect(self.target)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sessions")
            conn.executemany(
                "INSERT OR REPLACE INTO sessions"
                "(sid,source,workspace,title,created,updated,status) "
                "VALUES(?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
        return len(rows)

    def sessions_by_workspace(self, workspace: str, limit: int = 50) -> list:
        """按规范化工作区路径取会话（用于空壳项目详情兜底深链）。"""
        key = (workspace or "").replace("\\", "/").rstrip("/").lower()
        conn = _connect(self.target)
        try:
            rows = conn.execute(
                "SELECT sid, source, workspace, title, created, updated FROM sessions "
                "WHERE lower(replace(workspace,'\\','/')) = ? "
                "ORDER BY created DESC LIMIT ?", (key, max(1, limit))).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["link"] = _deep_link(item["sid"], item["source"])
                out.append(item)
            return out
        finally:
            conn.close()

    def count_sessions(self) -> int:
        conn = _connect(self.target)
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()


def _deep_link(sid: str, source: str) -> str:
    """深链生成放在这里做延迟导入，避免 report_store 与适配器包循环依赖。"""
    try:
        import archiver as A
        return A.deep_link_of(sid, source)
    except Exception:
        return f"workbuddy://chat/{sid}" if sid else ""


# ---------------------------------------------------------------- 迁移

def migrate_from_json(target: Path) -> dict:
    """把旧 report_index.json 的报告条目导入 SQLite（幂等，不删旧文件）。

    返回 {"reports": n, "sessions": m}。sessions 从本地各平台索引抽取。
    """
    init_schema(target)
    db = ReportDB(target)
    old = Path(target) / "report_index.json"
    entries = []
    try:
        data = json.loads(old.read_text(encoding="utf-8") or "{}")
        entries = data.get("reports", []) if isinstance(data, dict) else []
    except Exception:
        entries = []
    for entry in entries:
        db.upsert_report(entry)
    db.touch_updated()

    sessions = 0
    try:
        import archiver as A
        adapters = A.get_adapters()
        idx = A.build_index(adapters)
        sessions = db.replace_sessions(idx)
    except Exception:
        sessions = 0  # 会话抽取失败不影响报告迁移，可稍后单独跑
    return {"reports": len(entries), "sessions": sessions}
