#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨天账本 `stats` 的单元测试。

守的两条：
    1. **口径必须和 daylog 一致**——同一个数字在两处对不上，用户就再也不会相信任何一个
    2. **cache_read 是 input 的子集**，三个分量相加会把输入算成两倍（这个坑踩过两次）

运行：
    python scripts/tests/test_stats.py
    或 python -m unittest discover -s scripts/tests
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    _aggregate_day, _day_list, _group_label, _merge_group, _spark, cmd_stats,
    human_num, tok_total,
)


class FakeCol:
    """只带 _aggregate_day 需要的字段。"""

    def __init__(self, ws, sid, days, title="标题"):
        self.ws, self.sid, self.title = ws, sid, title
        self.source = "workbuddy"
        self.days = days


def bucket(prompts=(), tok=None, files=None):
    return {
        "prompts": [(1, p) for p in prompts],
        "files": files or {}, "tools": {"Write": 1}, "external": {},
        "first": "2026-11-19 10:00", "last": "2026-11-19 11:00",
        "chars": 0, "errors": 0, "wcalls": 1, "whits": 1, "exts": 0,
        "turns": [], "tcls": {"命令": 1}, "ccmd": {}, "cur": None,
        "tok": tok or {"in": 0, "out": 0, "cache_read": 0},
        "extfiles": {}, "extspan": {},
    }


class TestTokTotal(unittest.TestCase):
    def test_cache_read_not_added(self):
        """实测 total = input + output，cache_read 是 input 的子集。

        三个分量相加 => 输入算成两倍。第一版就是这样，每日合计比总数大一倍。
        """
        g = {"tok": {"in": 1000, "out": 200, "cache_read": 900}}
        self.assertEqual(tok_total(g), 1200)

    def test_missing_tok(self):
        self.assertEqual(tok_total({}), 0)


class TestAggregateAndMerge(unittest.TestCase):
    def setUp(self):
        self.col = FakeCol("/p/demo", "s1", {
            "d1": bucket(["第一条", "第二条"], {"in": 100, "out": 10, "cache_read": 90},
                         {"a.py": "write", "b.py": "edit"}),
            "d2": bucket(["第三条"], {"in": 50, "out": 5, "cache_read": 40},
                         {"a.py": "edit"}),
        })

    def test_single_day(self):
        groups, hits = _aggregate_day([self.col], "d1")
        self.assertEqual(hits, 1)
        g = groups["/p/demo"]
        self.assertEqual(len(g["prompts"]), 2)
        self.assertEqual(len(g["files"]), 2)
        self.assertEqual(tok_total(g), 110)

    def test_merge_sums_tokens_once(self):
        g1, _ = _aggregate_day([self.col], "d1")
        g2, _ = _aggregate_day([self.col], "d2")
        dst = archiver._empty_group("/p/demo")
        _merge_group(dst, g1["/p/demo"])
        _merge_group(dst, g2["/p/demo"])
        self.assertEqual(dst["tok"]["in"], 150)
        self.assertEqual(dst["tok"]["out"], 15)
        self.assertEqual(tok_total(dst), 165)      # 不是 150+15+130

    def test_merge_keeps_write_semantics(self):
        """同一天先 write 后 edit，合并后应记为 write（写入覆盖编辑）。"""
        g1, _ = _aggregate_day([self.col], "d1")
        g2, _ = _aggregate_day([self.col], "d2")
        dst = archiver._empty_group("/p/demo")
        _merge_group(dst, g1["/p/demo"])
        _merge_group(dst, g2["/p/demo"])
        self.assertEqual(dst["files"]["a.py"], "write")
        archiver._finalize_groups({"/p/demo": dst})
        self.assertEqual(dst["writes"] + dst["edits"], 2)

    def test_empty_day(self):
        groups, hits = _aggregate_day([self.col], "d3")
        self.assertEqual((groups, hits), ({}, 0))


class TestFormatting(unittest.TestCase):
    def test_day_list(self):
        self.assertEqual(_day_list("2026-11-19", 3), ["2026-11-17", "2026-11-18", "2026-11-19"])

    def test_spark_marks_peak(self):
        s = _spark([0, 5, 10])
        self.assertEqual(len(s), 3)
        self.assertEqual(s[-1], "█")

    def test_spark_all_zero_uses_dots(self):
        self.assertEqual(_spark([0, 0, 0]), "···")

    def test_human_num(self):
        self.assertEqual(human_num(999), "999")
        self.assertEqual(human_num(1500), "1.5k")
        self.assertEqual(human_num(2_400_000), "2.4M")

    def test_group_label_platform_dir_uses_title(self):
        g = {"ws": "C:/Users/x/WorkBuddy/2026-11-18-21-34-40",
             "sessions": [{"title": "本机日志集成方案"}]}
        self.assertEqual(_group_label(g), "本机日志集成方案")

    def test_group_label_normal_dir(self):
        g = {"ws": "D:/Projects/BatchRenamer", "sessions": [{"title": "随便"}]}
        self.assertEqual(_group_label(g), "BatchRenamer")


def canned():
    groups = {
        "/p/a": {"ws": "/p/a", "sessions": [{"sid": "s1", "title": "甲"}],
                 "prompts": [(1, "x"), (2, "y")], "files": {"a.py": "write"},
                 "tok": {"in": 1000, "out": 100, "cache_read": 900},
                 "writes": 1, "edits": 0},
        "/p/b": {"ws": "/p/b", "sessions": [{"sid": "s2", "title": "乙"}],
                 "prompts": [(3, "z")], "files": {"b.py": "edit"},
                 "tok": {"in": 500, "out": 50, "cache_read": 400},
                 "writes": 0, "edits": 1},
    }
    daily = [
        {"date": "2026-11-17", "hits": 0, "projects": 0, "prompts": 0, "files": 0,
         "tokens": 0, "cache_read": 0},
        {"date": "2026-11-18", "hits": 1, "projects": 1, "prompts": 2, "files": 1,
         "tokens": 1100, "cache_read": 900},
        {"date": "2026-11-19", "hits": 1, "projects": 1, "prompts": 1, "files": 1,
         "tokens": 550, "cache_read": 400},
    ]
    return {"ok": True, "stat": {}, "groups": groups, "daily": daily,
            "days": ["2026-11-17", "2026-11-18", "2026-11-19"]}


class TestCmdStats(unittest.TestCase):
    def setUp(self):
        self._orig = archiver.collect_range
        self.addCleanup(setattr, archiver, "collect_range", self._orig)
        archiver.collect_range = lambda days, sources=None: canned()

    def _run(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_stats(days=3, end="2026-11-19", **kw)
        return rc, buf.getvalue()

    def test_json_totals(self):
        rc, out = self._run(as_json=True)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        t = data["totals"]
        self.assertEqual(t["prompts"], 3)
        self.assertEqual(t["sessions"], 2)          # 按 sid 去重
        self.assertEqual(t["projects"], 2)
        self.assertEqual(t["tok_in"], 1500)
        self.assertEqual(t["tok_out"], 150)
        self.assertEqual(t["cache_rate"], 86.7)
        self.assertEqual(data["blank_days"], ["2026-11-17"])

    def test_text_does_not_estimate_cost(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("不估算", out)
        self.assertIn("空白天", out)
        # 不能出现任何货币符号——估成本等于编数字
        self.assertNotIn("¥", out)

    def test_bad_date(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_stats(days=3, end="2026/11/19")
        self.assertEqual(rc, 1)

    def test_chinese_header_alignment(self):
        """表头与数据行必须同宽：中文是双宽字符，按字符数补空格会歪。"""
        _, out = self._run()
        lines = [x for x in out.splitlines() if x.startswith("  日期") or
                 x.startswith("  11-")]
        self.assertEqual(len(lines), 4)            # 表头 + 3 行
        widths = {archiver._dw(x) for x in lines}
        self.assertEqual(len(widths), 1, f"宽度不一致：{widths}")


if __name__ == "__main__":
    unittest.main()
