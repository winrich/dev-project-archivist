"""报告档案索引测试：登记/去重/存量补登/按日期检索/白名单。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as A
import manage_store as M
import manage_ui as UI


def _fake_report(target, name, text="<html>report</html>"):
    p = target / A.REPORT_SUBDIR / name
    A.atomic_write(p, text)
    return p


def _d(frm, to, prompts=10, sessions=2, projects=3, files=4, tok_in=100, tok_out=5):
    return {"from": frm, "to": to, "days": 30,
            "totals": {"prompts": prompts, "sessions": sessions, "projects": projects,
                       "files": files, "tok_in": tok_in, "tok_out": tok_out}}


def test_index_new_and_idempotent():
    """同区间重复登记覆盖条目，不产生重复；异区间各一条。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        p = _fake_report(target, "report-2026-10-21_2026-11-19.html", "A" * 100)
        with A.Lock(target):
            assert A.index_report(target, _d("2026-10-21", "2026-11-19", prompts=10), p) is True
            assert A.index_report(target, _d("2026-10-21", "2026-11-19", prompts=20), p) is False
        idx = A.load_report_index(target)
        assert len(idx["reports"]) == 1, idx
        assert idx["reports"][0]["prompts"] == 20, idx  # 被覆盖为最新
        assert idx["reports"][0]["tokens"] == 105
        assert idx["reports"][0]["sha256"] == A.sha1_file(p)
        print("  登记/覆盖/摘要/指纹 OK")


def test_backfill():
    """存量 HTML 补登记，只认合法文件名，跳过已登记。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        _fake_report(target, "report-2026-10-01_2026-10-30.html")
        _fake_report(target, "evil.html")  # 非法名，应跳过
        with A.Lock(target):
            n = A.backfill_report_index(target)
            n2 = A.backfill_report_index(target)  # 再跑一次应 0
        assert n == 1, n
        assert n2 == 0, n2
        idx = A.load_report_index(target)
        assert len(idx["reports"]) == 1
        assert idx["reports"][0]["from"] == "2026-10-01"
        assert idx["reports"][0]["prompts"] == 0  # 存量摘要无法反推
        print("  存量补登/去重/非法名跳过 OK")


def test_report_archive_by_date():
    """按日期检索：区间覆盖命中、边界外排除、非法日期返回空。"""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        p1 = _fake_report(target, "report-2026-11-01_2026-11-10.html")
        p2 = _fake_report(target, "report-2026-11-11_2026-11-20.html")
        with A.Lock(target):
            A.index_report(target, _d("2026-11-01", "2026-11-10"), p1)
            A.index_report(target, _d("2026-11-11", "2026-11-20"), p2)
        s = M.ManageStore(target)
        hit = s.report_archive("2026-11-05")
        assert len(hit) == 1 and hit[0]["from"] == "2026-11-01", hit
        assert hit[0]["day_index"] == 5, hit
        assert len(s.report_archive("2026-10-31")) == 0
        assert len(s.report_archive("bad-date")) == 0
        # 跨两份报告的重叠日期（若区间相邻不重叠则各一）
        assert len(s.report_archive("2026-11-15")) == 1
        lst = s.report_list()
        assert lst["total"] == 2
        assert lst["items"][0]["to"] == "2026-11-20"  # 倒序，最新在前
        print("  日期检索/边界/倒序/分页元数据 OK")


def test_filename_whitelist():
    """serve 文件名白名单：合法通过，路径穿越与任意名拒绝。"""
    assert UI.report_name_ok("report-2026-10-21_2026-11-19.html") is True
    for bad in ("", "evil.html", "../state.json", "report-2026-10-21_2026-11-19.html/../../x",
                "report-20261021_20261119.html", "report-2026-10-21_2026-11-19.HTML"):
        assert UI.report_name_ok(bad) is False, bad
    print("  文件名白名单 OK")


def main():
    test_index_new_and_idempotent()
    test_backfill()
    test_report_archive_by_date()
    test_filename_whitelist()
    print("ALL OK")


if __name__ == "__main__":
    main()
