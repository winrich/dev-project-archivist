"""报告档案 SQLite 存储测试：登记/去重/迁移/按日期检索/会话/空壳兜底/白名单。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as A
import manage_store as M
import manage_ui as UI
import report_store as R


def _fake_report(target, name, text="<html>report</html>"):
    p = target / A.REPORT_SUBDIR / name
    A.atomic_write(p, text)
    return p


def _entry(name, frm, to, prompts=10, sessions=2, projects=3, files=4, tok=105):
    return {"name": name, "source": "all", "from": frm, "to": to, "days": 30,
            "generated_at": "2026-11-19T10:00:00", "prompts": prompts,
            "sessions": sessions, "projects": projects, "files": files,
            "tokens": tok, "sha256": "abc", "bytes": 100}


class _FakeIndex:
    """模拟 MultiIndex：ids/info 两个方法即可。"""
    def __init__(self, rows):
        self.rows = rows
    def ids(self):
        return {sid for sid, _ in self.rows}
    def info(self, sid):
        for s, info in self.rows:
            if s == sid:
                return info
        return {}


def test_sqlite_upsert_idempotent():
    """SQLite 登记幂等：同 name 覆盖，异 name 各一条，摘要/指纹正确。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        db = R.ReportDB(target)
        R.init_schema(target)
        e = _entry("report-2026-10-21_2026-11-19.html", "2026-10-21", "2026-11-19")
        db.upsert_report(e)
        db.upsert_report({**e, "prompts": 20})
        assert db.count_reports() == 1
        got = db.list_reports()["items"][0]
        assert got["prompts"] == 20, got
        assert got["to"] == "2026-11-19"
        print("  SQLite 登记/覆盖 OK")


def test_report_archive_by_date():
    """按日期检索：区间覆盖命中、边界外排除、非法日期空、倒序、分页。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        R.init_schema(target)
        db = R.ReportDB(target)
        db.upsert_report(_entry("r1.html", "2026-11-01", "2026-11-10"))
        db.upsert_report(_entry("r2.html", "2026-11-11", "2026-11-20"))
        s = M.ManageStore(target)
        hit = s.report_archive("2026-11-05")
        assert len(hit) == 1 and hit[0]["from"] == "2026-11-01", hit
        assert hit[0]["day_index"] == 5, hit
        assert len(s.report_archive("2026-10-31")) == 0
        assert len(s.report_archive("bad-date")) == 0
        lst = s.report_list()
        assert lst["total"] == 2
        assert lst["items"][0]["to"] == "2026-11-20"  # 倒序
        assert len(s.report_list(offset=1, limit=1)["items"]) == 1  # 分页
        print("  日期检索/边界/倒序/分页 OK")


def test_migrate_from_json():
    """JSON 索引迁移幂等：跑两次不重复，旧文件保留，会话从索引抽取。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        A.atomic_write(target / "report_index.json",
                       '{"reports":[' + __import__("json").dumps(
                           _entry("report-2026-01-01_2026-01-30.html",
                                  "2026-01-01", "2026-01-30")) + ']}')
        r1 = R.migrate_from_json(target)
        r2 = R.migrate_from_json(target)
        assert r1["reports"] == 1 and r2["reports"] == 1
        assert R.ReportDB(target).count_reports() == 1
        assert (target / "report_index.json").exists()  # 旧文件未删
        print("  JSON 迁移幂等/保留旧文件 OK")


def test_sessions_and_workspace():
    """会话表全量替换 + 按工作区查 + 深链生成。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        R.init_schema(target)
        db = R.ReportDB(target)
        ix = _FakeIndex([
            ("sid-1", {"source": "workbuddy", "cwd": "C:/ws/proj",
                       "title": "标题", "created": 1794551391964, "updated": 1794551391964}),
            ("sid-2", {"source": "workbuddy", "cwd": "D:/other",
                       "title": "另一个", "created": 1794551400000, "updated": 0}),
        ])
        assert db.replace_sessions(ix) == 2
        assert db.count_sessions() == 2
        got = db.sessions_by_workspace("c:\\ws\\proj\\")
        assert len(got) == 1 and got[0]["sid"] == "sid-1", got
        assert got[0]["link"] == "workbuddy://chat/sid-1", got
        # 再次替换应仍是 2（全量替换幂等）
        assert db.replace_sessions(ix) == 2
        assert db.count_sessions() == 2
        print("  会话表/工作区检索/深链 OK")


def test_empty_shell_detail_fallback():
    """空壳项目无归档目录时，详情从 sessions 表兜底生成对话链接。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        # 造一个 state.json：空壳项目，无 dir
        ws = "C:/Users/name/WorkBuddy/shell-proj"  # 用虚构用户名，避免触发防泄漏测试
        state = {"projects": {ws.lower(): {
            "type": "空壳", "name": "空壳项目", "workspace": ws,
            "sessions": 1, "files": 0, "bytes": 0, "archived": False,
            "first_ts": 1794551391964, "last_ts": 1794551391964}}}
        A.atomic_write(target / A.STATE_FILE, __import__("json").dumps(state, ensure_ascii=False))
        R.init_schema(target)
        db = R.ReportDB(target)
        db.replace_sessions(_FakeIndex([
            ("sid-shell", {"source": "workbuddy", "cwd": ws,
                           "title": "空壳的对话", "created": 1794551391964, "updated": 0}),
        ]))
        d = M.ManageStore(target).detail(ws.lower())
        assert len(d["conversations"]) == 1, d["conversations"]
        assert d["conversations"][0]["link"] == "workbuddy://chat/sid-shell", d
        print("  空壳详情对话兜底 OK")


def test_legacy_json_index_still_written():
    """双写：index_report 仍写 JSON 镜像（回退源保留）。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        p = _fake_report(target, "report-2026-10-21_2026-11-19.html")
        with A.Lock(target):
            A.index_report(target, _d("2026-10-21", "2026-11-19"), p)
        idx = A.load_report_index(target)
        assert len(idx["reports"]) == 1 and idx["reports"][0]["prompts"] == 10
        print("  JSON 镜像仍写入 OK")


def _d(frm, to, prompts=10, sessions=2, projects=3, files=4, tok_in=100, tok_out=5):
    return {"from": frm, "to": to, "days": 30,
            "totals": {"prompts": prompts, "sessions": sessions, "projects": projects,
                       "files": files, "tok_in": tok_in, "tok_out": tok_out}}


def test_filename_whitelist():
    """serve 文件名白名单：合法通过，路径穿越与任意名拒绝。"""
    assert UI.report_name_ok("report-2026-10-21_2026-11-19.html") is True
    for bad in ("", "evil.html", "../state.json",
                "report-2026-10-21_2026-11-19.html/../../x",
                "report-20261021_20261119.html", "report-2026-10-21_2026-11-19.HTML"):
        assert UI.report_name_ok(bad) is False, bad
    print("  文件名白名单 OK")


def main():
    test_sqlite_upsert_idempotent()
    test_report_archive_by_date()
    test_migrate_from_json()
    test_sessions_and_workspace()
    test_empty_shell_detail_fallback()
    test_legacy_json_index_still_written()
    test_filename_whitelist()
    print("ALL OK")


if __name__ == "__main__":
    main()
