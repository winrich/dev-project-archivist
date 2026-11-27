"""活跃度算法单元测试：固定 today，逐维度验证公式与降级行为。"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as A
import manage_store as M


TODAY = datetime(2026, 11, 19, 12, 0, 0)


def make_dir(root, conv_dates):
    """造一个最小归档目录，conversations.md 按给定日期写入会话。"""
    adir = root / "proj"
    (adir / A.META_DIR).mkdir(parents=True, exist_ok=True)
    lines = ["# 原始对话索引", ""]
    for i, d in enumerate(conv_dates, 1):
        lines += [f"## {i}. 会话{i}", "",
                  "| 项 | 值 |", "|---|---|", f"| 时间 | {d} 09:00 |", ""]
    A.atomic_write(adir / A.META_DIR / A.CONVERSATIONS_FILE, "\n".join(lines))
    return adir


def test_recency():
    """近期性：30 天减半，60 天再减半；今天满分。"""
    with tempfile.TemporaryDirectory() as td:
        adir = make_dir(Path(td), [])
        base = {"sessions": 0, "files": 0, "last_ts": None}
        today_ms = int(TODAY.timestamp() * 1000)
        a0 = M.activity_of({**base, "last_ts": today_ms}, adir, TODAY)
        a30 = M.activity_of({**base, "last_ts": today_ms - 30*86400000}, adir, TODAY)
        r0 = a0["parts"]["recency"]
        r30 = a30["parts"]["recency"]
        assert r0 == 100.0, r0
        assert abs(r30 - 50.0) < 0.5, r30
        print(f"  近期性: 今天={r0} 30天前={r30}")


def test_freq_output_log():
    """频次/产出：对数压缩，1→25/12，_session 多也不线性爆炸。"""
    with tempfile.TemporaryDirectory() as td:
        adir = make_dir(Path(td), [])
        base = {"last_ts": int(TODAY.timestamp()*1000)}
        a1 = M.activity_of({**base, "sessions": 1, "files": 1}, adir, TODAY)
        a15 = M.activity_of({**base, "sessions": 15, "files": 46}, adir, TODAY)
        assert abs(a1["parts"]["freq"] - 25.0) < 0.5, a1["parts"]
        assert a15["parts"]["freq"] >= 100.0, a15["parts"]
        assert 66 <= a15["parts"]["output"] <= 68, a15["parts"]
        print(f"  对数压缩: 1次freq={a1['parts']['freq']}, "
              f"15次freq={a15['parts']['freq']}, 46文件output={a15['parts']['output']}")


def test_consistency_and_spark():
    """持续度：不同活跃天数；迷你图按近 14 天聚合，超出窗口不计入 spark 但计入持续度。"""
    with tempfile.TemporaryDirectory() as td:
        dates = ["2026-11-19", "2026-11-18", "2026-11-10", "2026-01-01"]
        adir = make_dir(Path(td), dates)
        st = {"sessions": 4, "files": 3,
              "last_ts": int(TODAY.timestamp()*1000)}
        a = M.activity_of(st, adir, TODAY)
        assert a["active_days"] == 4, a["active_days"]
        assert abs(a["parts"]["consistency"] - 40.0) < 0.5, a["parts"]
        vals = {x["date"]: x["count"] for x in a["spark"]}
        assert vals["2026-11-19"] == 1 and vals["2026-11-18"] == 1, vals
        assert vals["2026-11-10"] == 1, vals
        assert "2026-01-01" not in vals, vals
        assert sum(x["count"] for x in a["spark"]) == 3, vals
        print(f"  持续度={a['parts']['consistency']} 活跃天数={a['active_days']} "
              f"窗口内会话={sum(x['count'] for x in a['spark'])}")


def test_degrade_without_conv():
    """降级：无 conversations.md 时持续度=0、迷你图全零，其它维度照常。"""
    with tempfile.TemporaryDirectory() as td:
        a = M.activity_of({"sessions": 5, "files": 5,
                           "last_ts": int(TODAY.timestamp()*1000)},
                          Path(td) / "missing", TODAY)
        assert a["parts"]["consistency"] == 0.0
        assert all(x["count"] == 0 for x in a["spark"])
        assert a["score"] > 0
        print(f"  降级: score={a['score']} consistency=0 spark全零")


def test_levels():
    """分档边界：hot/active/calm/idle。"""
    expected = {70: "hot", 45: "active", 20: "calm", 0: "idle"}
    for score, name in expected.items():
        got = next(n for t, n in M.ACT_LEVELS if score >= t)
        assert got == name, (score, got)
    print("  分档边界: 70/45/20/0 → hot/active/calm/idle")


def main():
    tests = [test_recency, test_freq_output_log, test_consistency_and_spark,
             test_degrade_without_conv, test_levels]
    for t in tests:
        print("·", t.__name__)
        t()
    print(f"\n通过 {len(tests)} / {len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
