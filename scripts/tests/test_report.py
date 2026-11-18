#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report 的测试。

重点不是"HTML 里有几个 div"，而是报告要发出去这件事的三条底线：
  1. 任何动态文本都必须转义（项目名来自用户目录名，什么都可能有）
  2. 不能带工作区绝对路径（含用户名、内网目录）
  3. 不能带指令原文（可能含代码、密钥、私事）
再加一条：同样的数据必须产出逐字节相同的文件，否则"每天跑一次"会变成"每天多一个文件"。
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    REPORT_PROJECTS_MAX, _heat_level, _rep_bars, _rep_heat, _rep_projects,
    build_report_html, cmd_report, report_data, report_label,
)


def _day(date_str, prompts=1, files=1, tokens=1000, hits=1):
    return {"date": date_str, "hits": hits, "projects": 1 if hits else 0,
            "prompts": prompts, "files": files, "tokens": tokens,
            "cache_read": 0}


def sample_data(**over):
    d = {
        "ok": True, "from": "2026-11-01", "to": "2026-11-03", "days": 3,
        "totals": {"prompts": 12, "sessions": 2, "projects": 2, "files": 5,
                   "writes": 4, "edits": 1, "tok_in": 1200000, "tok_out": 34000,
                   "cache_read": 1100000, "cache_rate": 91.7},
        "daily": [_day("2026-11-01", 4), _day("2026-11-02", 0, 0, 0, 0),
                  _day("2026-11-03", 8)],
        "projects": [
            {"name": "归档器", "prompts": 8, "files": 3, "sessions": 1,
             "tokens": 900000, "first": "2026-11-01", "last": "2026-11-03"},
            {"name": "example.org", "prompts": 4, "files": 2, "sessions": 1,
             "tokens": 300000, "first": "2026-11-02", "last": "2026-11-02"},
        ],
        "blank": ["2026-11-02"],
    }
    d.update(over)
    return d


def _group(ws, prompts=(), files=None, first="", last=""):
    g = archiver._empty_group(ws)
    g["prompts"] = list(prompts)
    g["files"] = dict(files or {})
    g["first"], g["last"] = first, last
    return g


class TestEscape(unittest.TestCase):
    def test_script_tag_escaped(self):
        html = build_report_html(sample_data(projects=[
            {"name": "<script>alert(1)</script>", "prompts": 1, "files": 1,
             "sessions": 1, "tokens": 1, "first": "2026-11-01",
             "last": "2026-11-01"}]))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_quote_escaped_in_attribute(self):
        # 项目名会进 title 属性，双引号必须转义，否则能提前闭合属性
        html = build_report_html(sample_data(daily=[
            _day('2026-11-01"onmouseover="alert(1)')]))
        self.assertNotIn('"onmouseover="alert(1)"', html)
        self.assertIn("&quot;onmouseover=&quot;", html)

    def test_truncation_adds_ellipsis(self):
        out = archiver._rep("一" * 60, 10)
        self.assertEqual(len(out), 11)
        self.assertTrue(out.endswith("…"))


class TestReportLabel(unittest.TestCase):
    def test_plain_dir_name(self):
        self.assertEqual(report_label(_group("/home/u/my-project")), "my-project")

    def test_sensitive_dir_hidden(self):
        # .ssh 这种目录名本身就是信息，报告要发出去，宁可显示"已隐藏"
        self.assertIn("已隐藏", report_label(_group(r"C:\Users\me\.ssh")))
        self.assertIn("已隐藏", report_label(_group("/home/u/.aws")))

    def test_token_in_name_redacted(self):
        # 只认完整键名（access_token / api_key），裸 token 是提示词里的常用词，
        # 一律抹掉会把正文改烂——这条边界由 desensitize 统一决定
        out = report_label(_group("/home/u/proj-access_token=abc123456789"))
        self.assertNotIn("abc123456789", out)
        self.assertIn("<REDACTED>", out)

    def test_sk_key_in_name_redacted(self):
        out = report_label(_group("/home/u/sk-abcdefghijklmnop1234-demo"))
        self.assertNotIn("abcdefghijklmnop1234", out)

    def test_platform_dir_falls_back_to_title(self):
        g = _group("C:/Users/me/WorkBuddy/2026-11-18-21-34-40")
        g["sessions"] = [{"title": "重构归档器"}]
        self.assertEqual(report_label(g), "重构归档器")

    def test_long_name_truncated(self):
        self.assertLessEqual(len(report_label(_group("/home/u/" + "x" * 80))), 36)


class TestHeat(unittest.TestCase):
    def test_level_boundaries(self):
        self.assertEqual(_heat_level(0, 100), 0)
        self.assertEqual(_heat_level(10, 100), 1)
        self.assertEqual(_heat_level(40, 100), 2)
        self.assertEqual(_heat_level(70, 100), 3)
        self.assertEqual(_heat_level(100, 100), 4)

    def test_zero_top_is_safe(self):
        self.assertEqual(_heat_level(0, 0), 0)

    def test_offset_matches_weekday(self):
        dates = ["2026-11-02", "2026-11-03"]        # 11-02 是周一
        cells = _rep_heat([_day(d) for d in dates])
        expected = datetime.strptime(dates[0], "%Y-%m-%d").weekday()
        self.assertEqual(cells.count('class="blank"'), expected)
        self.assertEqual(cells.count("<i class="), expected + len(dates))

    def test_empty_daily_returns_empty(self):
        self.assertEqual(_rep_heat([]), "")


class TestBars(unittest.TestCase):
    def test_one_rect_per_day(self):
        daily = [_day(f"2026-11-{i:02d}", i) for i in range(1, 6)]
        self.assertEqual(_rep_bars(daily).count("<rect"), 5)

    def test_all_zero_still_renders(self):
        # 全零也要画出来：否则用户以为图表坏了
        daily = [_day(f"2026-11-{i:02d}", 0, 0, 0, 0) for i in range(1, 4)]
        self.assertEqual(_rep_bars(daily).count("<rect"), 3)

    def test_empty_daily(self):
        self.assertEqual(_rep_bars([]), "")


class TestProjects(unittest.TestCase):
    def test_empty_state(self):
        self.assertIn("没有记录到开发活动", _rep_projects([]))

    def test_capped_with_note(self):
        projects = [{"name": f"p{i}", "prompts": i + 1, "files": 1, "sessions": 1,
                     "tokens": 1, "first": "2026-11-01", "last": "2026-11-01"}
                    for i in range(REPORT_PROJECTS_MAX + 5)]
        out = _rep_projects(projects)
        self.assertEqual(out.count("<li>"), REPORT_PROJECTS_MAX)
        self.assertIn("另有 5 个项目未展开", out)


class TestBuildHtml(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(build_report_html(sample_data()),
                         build_report_html(sample_data()))

    def test_no_absolute_path(self):
        html = build_report_html(sample_data())
        for bad in ("C:\\", "/home/", "Users\\", "Users/"):
            self.assertNotIn(bad, html)

    def test_single_file_no_scripts_no_external_assets(self):
        html = build_report_html(sample_data())
        self.assertNotIn("<script", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("src=", html)
        # 唯一允许的外部引用是页脚里的仓库地址（纯文本链接，不加载资源）
        self.assertEqual(html.count("http"), 1)

    def test_totals_rendered(self):
        html = build_report_html(sample_data())
        self.assertIn("12", html)          # 条对话
        self.assertIn("1.2M", html)        # token 合计 1.234M → 1.2M
        self.assertIn("92%", html)         # 缓存命中 91.7 → 四舍五入 92%

    def test_no_token_data_message(self):
        d = sample_data()
        d["totals"].update({"tok_in": 0, "tok_out": 0, "cache_read": 0,
                            "cache_rate": 0.0})
        self.assertIn("Token 未记录", build_report_html(d))

    def test_blank_days_summarised(self):
        html = build_report_html(sample_data())
        self.assertIn("其中 1 天没有会话", html)

    def test_short_range_uses_fact_cards_not_heatmap(self):
        # 三天也画热力图就是一个格子，看起来像页面坏了
        html = build_report_html(sample_data())
        self.assertNotIn('class="heat"', html)
        self.assertIn('class="facts h"', html)

    def test_long_range_uses_heatmap(self):
        d = sample_data()
        d["days"] = 30
        d["daily"] = [_day(f"2026-11-{i:02d}", i) for i in range(1, 31)]
        html = build_report_html(d)
        self.assertIn('class="heat"', html)
        self.assertIn("每格一天", html)


class TestReportData(unittest.TestCase):
    def test_shares_collect_range(self):
        """report 必须走 collect_range，不能自己另算一遍口径。"""
        calls = []

        def fake(dates, sources=None, merge_external=False):
            calls.append(list(dates))
            return {"ok": True, "stat": {}, "adapters": [],
                    "daily": [_day(dates[0]), _day(dates[-1])],
                    "groups": {"a": _group("C:/w/proj-a", [("1", "改登录逻辑")],
                                           {"a.cs": "write"}, 1000, 2000)}}

        old = archiver.collect_range
        archiver.collect_range = fake
        try:
            d = report_data(days=2, end="2026-11-03")
        finally:
            archiver.collect_range = old
        self.assertTrue(d["ok"])
        self.assertEqual(calls, [["2026-11-02", "2026-11-03"]])
        self.assertEqual(d["projects"][0]["name"], "proj-a")
        self.assertEqual(d["projects"][0]["prompts"], 1)

    def test_bad_date(self):
        self.assertFalse(report_data(days=3, end="11/03/2026")["ok"])


class TestCmdReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-report-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _patch(self):
        def fake(dates, sources=None, merge_external=False):
            return {"ok": True, "stat": {}, "adapters": [],
                    "daily": [_day("2026-11-01", 4), _day("2026-11-02", 8)],
                    "groups": {"a": _group("C:/w/alpha", [("1", "x")],
                                           {"m.cs": "write"}, 1000, 2000)}}
        self._old = archiver.collect_range
        archiver.collect_range = fake

    def _unpatch(self):
        archiver.collect_range = self._old

    def _run(self, **kw):
        """跑一次 cmd_report，并把它的控制台输出收走，免得污染测试输出。"""
        with contextlib.redirect_stdout(io.StringIO()):
            return cmd_report(self.tmp, **kw)

    def test_writes_one_file_named_by_range(self):
        self._patch()
        try:
            rc = self._run(days=2, end="2026-11-02")
        finally:
            self._unpatch()
        self.assertEqual(rc, 0)
        files = list((self.tmp / "report").glob("*.html"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "report-2026-11-01_2026-11-02.html")

    def test_idempotent_bytes(self):
        self._patch()
        try:
            self._run(days=2, end="2026-11-02")
            p = self.tmp / "report" / "report-2026-11-01_2026-11-02.html"
            first = p.read_bytes()
            self._run(days=2, end="2026-11-02")
            self.assertEqual(p.read_bytes(), first)
        finally:
            self._unpatch()

    def test_out_dir_takes_precedence(self):
        self._patch()
        try:
            out = self.tmp / "custom"
            self._run(days=2, end="2026-11-02", out=str(out))
        finally:
            self._unpatch()
        self.assertTrue((out / "report-2026-11-01_2026-11-02.html").exists())
        self.assertFalse((self.tmp / "report").exists())


if __name__ == "__main__":
    unittest.main()
