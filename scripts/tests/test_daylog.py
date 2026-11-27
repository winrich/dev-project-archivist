#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daylog 按天聚合的单元测试。

核心验收标准只有一条：**不丢不重**。
    各日指令数之和 == 该会话去重后的用户指令总数

其余几条守的是「日志内容对不对」：编辑动作要留下路径、压缩重放要压掉、
工作区外写入要单独计数而不能混进工作区。

运行：
    python scripts/tests/test_daylog.py
    或 python -m unittest discover -s scripts/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import DayCollector  # noqa: E402

CWD = "/work/demo-app"


def ev(kind, **kw):
    d = {"kind": kind}
    d.update(kw)
    return d


# 跨三天的会话：每天两条指令，中间夹一次 Write 与一次 Edit。
EVENTS = [
    ev("ts", value="2026-11-18T09:00:00Z"),
    ev("prompt", text="先把编译错误修掉"),
    ev("tool", name="Edit"),
    ev("write", rel="src/a.cs", op="edit", content=""),
    ev("ts", value="2026-11-18T10:30:00Z"),
    ev("prompt", text="再补一个单元测试"),
    ev("tool", name="Write"),
    ev("write", rel="tests/t.cs", op="write", content="class T {}"),

    ev("ts", value="2026-11-19T14:00:00Z"),
    ev("prompt", text="改一下界面布局"),
    ev("tool", name="Edit"),
    ev("write", rel="src/b.xaml", op="edit", content=""),
    ev("tool", name="Write"),
    ev("write", rel="", external="/fake/Temp/tmp1.tmp", op="write"),
    ev("ts", value="2026-11-19T16:00:00Z"),
    ev("prompt", text="颜色再深一点"),

    ev("ts", value="2026-11-20T08:00:00Z"),
    ev("prompt", text="把桌面端和移动端的打包脚本统一起来，顺便补一条 CI"),
    ev("prompt", text="把桌面端和移动端的打包脚本统一起来，顺便补一条 CI"),   # 原文重放，应被压掉
    ev("tool", name="Write"),
    ev("write", rel="build/p.ps1", op="write", content="exit 0"),
]


def collect(events, sid="s1"):
    c = DayCollector(CWD, sid, "workbuddy", "demo")
    c.consume(events)
    return c


class DayBucketTest(unittest.TestCase):
    def test_split_by_day_without_loss(self):
        """各日指令数之和 == 去重后总数（不丢不重）。"""
        c = collect(EVENTS)
        per_day = {d: len(b["prompts"]) for d, b in c.days.items() if b["prompts"]}
        self.assertEqual(per_day, {"2026-11-18": 2, "2026-11-19": 2, "2026-11-20": 1})
        self.assertEqual(sum(per_day.values()), 5)

    def test_files_follow_their_day(self):
        """文件归属于事件发生的那一天，而不是会话首末时间。"""
        c = collect(EVENTS)
        self.assertEqual(sorted(c.days["2026-11-18"]["files"]), ["src/a.cs", "tests/t.cs"])
        self.assertEqual(sorted(c.days["2026-11-19"]["files"]), ["src/b.xaml"])
        self.assertEqual(sorted(c.days["2026-11-20"]["files"]), ["build/p.ps1"])

    def test_edit_keeps_path_without_content(self):
        """Edit 只有路径没有正文，也必须记进文件清单。"""
        c = collect(EVENTS)
        self.assertEqual(c.days["2026-11-18"]["files"]["src/a.cs"], "edit")
        # 归档侧不认编辑正文，所以 written 里不应出现它
        p = archiver.SessionParser(Path("x.jsonl"), CWD)
        p.consume(iter(EVENTS))
        self.assertIn("tests/t.cs", p.written)
        self.assertNotIn("src/a.cs", p.written)
        self.assertIn("src/a.cs", p.touched)

    def test_external_write_counted_separately(self):
        """工作区外写入按目录计数，且不污染当天的文件清单。"""
        c = collect(EVENTS)
        b = c.days["2026-11-19"]
        self.assertEqual(b["external"].get("Temp"), 1)
        self.assertNotIn("/fake/Temp/tmp1.tmp", b["files"])
        self.assertEqual(c.write_calls, 5)
        self.assertEqual(c.write_hits, 4)
        self.assertEqual(c.external_hits, 1)

    def test_duplicate_prompt_compressed(self):
        """上下文压缩会把早期提问原样重放，长指令按指纹压重。"""
        c = collect(EVENTS)
        self.assertEqual(c.dup, 1)
        self.assertEqual(len(c.days["2026-11-20"]["prompts"]), 1)

    def test_short_repeat_kept(self):
        """短指令（继续/好）是真实动作，不能当重复压掉。"""
        c = collect([ev("ts", value="2026-11-18T09:00:00Z"),
                     ev("prompt", text="继续"), ev("prompt", text="继续")])
        self.assertEqual(c.dup, 0)
        self.assertEqual(len(c.days["2026-11-18"]["prompts"]), 2)

    def test_events_before_any_ts_are_untimed(self):
        """没有前置时间戳的事件无法归日，必须计入 untimed 而不是塞给某一天。"""
        c = collect([ev("prompt", text="没有时间戳的指令"),
                     ev("ts", value="2026-11-18T09:00:00Z"),
                     ev("prompt", text="有时间的指令")])
        self.assertEqual(c.untimed, 1)
        self.assertEqual(len(c.days["2026-11-18"]["prompts"]), 1)

    def test_no_events_means_empty(self):
        c = collect([])
        self.assertEqual(c.days, {})
        self.assertEqual(c.untimed, 0)


class CleanPromptTest(unittest.TestCase):
    def test_compaction_summary_is_dropped(self):
        """上下文压缩摘要会被平台存成用户消息，不能当成提问。"""
        from adapters.base import clean_prompt
        self.assertEqual(
            clean_prompt("Summary of the conversation between an AI agent and a user. All tool calls..."),
            "")
        self.assertEqual(clean_prompt("This session is being continued from a previous conversation."), "")
        # 正常提问不受影响
        self.assertEqual(clean_prompt("帮我把这个函数改成异步"), "帮我把这个函数改成异步")


class DayRangeTest(unittest.TestCase):
    def test_range_is_half_open(self):
        """日期区间左闭右开，保证相邻两天不重叠。"""
        s1, e1 = archiver.day_range_ms("2026-11-18")
        s2, _ = archiver.day_range_ms("2026-11-19")
        self.assertEqual(e1, s2)
        self.assertLess(s1, e1)

    def test_to_ms_accepts_seconds_and_iso(self):
        self.assertEqual(archiver.to_ms(1783940577), 1783940577000)
        self.assertEqual(archiver.to_ms(1783940577322), 1783940577322)
        self.assertEqual(archiver.to_ms("2026-11-18T00:00:00Z"),
                         archiver.to_ms("2026-11-17T16:00:00Z") + 8 * 3600 * 1000)
        self.assertIsNone(archiver.to_ms(""))
        self.assertIsNone(archiver.to_ms("not-a-date"))


class TurnTest(unittest.TestCase):
    """轮次视图：把「一句话」和「它带来的产出」在采集阶段就绑在一起。"""

    def test_files_attach_to_their_prompt(self):
        c = collect(EVENTS)
        turns = c.days["2026-11-18"]["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "先把编译错误修掉")
        self.assertEqual(turns[0]["files"], ["src/a.cs"])
        self.assertEqual(turns[1]["files"], ["tests/t.cs"])

    def test_turn_keeps_own_time(self):
        c = collect(EVENTS)
        turns = c.days["2026-11-19"]["turns"]
        self.assertEqual(turns[0]["t"], "14:00")
        self.assertEqual(turns[1]["t"], "16:00")

    def test_events_before_first_prompt_are_not_a_turn(self):
        """开头没有指令就先写盘的会话，不能凭空造一个空轮次。"""
        c = collect([ev("ts", value="2026-11-18T09:00:00Z"),
                     ev("write", rel="init.py", op="write", content="x"),
                     ev("prompt", text="开始吧")])
        b = c.days["2026-11-18"]
        self.assertEqual(len(b["turns"]), 1)
        self.assertEqual(b["turns"][0]["files"], [])
        self.assertIn("init.py", b["files"])

    def test_turn_counts_are_additive(self):
        """每天各轮次的产出量之和 == 当天总量，不能有事件既算又漏。"""
        c = collect(EVENTS)
        b = c.days["2026-11-18"]
        per_turn = sum(len(t["files"]) for t in b["turns"])
        self.assertEqual(per_turn, len(b["files"]))


class ToolTypeTest(unittest.TestCase):
    def test_known_names(self):
        from adapters.base import tool_type
        self.assertEqual(tool_type("Write"), "write")
        self.assertEqual(tool_type("MultiEdit"), "write")
        self.assertEqual(tool_type("Bash"), "shell")
        self.assertEqual(tool_type("run_command"), "shell")
        self.assertEqual(tool_type("Grep"), "search")
        self.assertEqual(tool_type("WebSearch"), "search")
        self.assertEqual(tool_type("Read"), "read")

    def test_snake_and_camel_are_same_bucket(self):
        """表里写的是 snake_case，查表时已被抹平，两者必须落到同一类。"""
        from adapters.base import tool_type
        self.assertEqual(tool_type("list_dir"), tool_type("ListDir"))
        self.assertEqual(tool_type("list_dir"), "search")
        self.assertEqual(tool_type("run_command"), tool_type("RunCommand"))

    def test_dispatcher_is_not_shell(self):
        """工具调度器名字里带 exec，但它不是跑命令——误判会让「命令数」虚高。"""
        from adapters.base import tool_type
        self.assertEqual(tool_type("DeferExecuteTool"), "generic")

    def test_unknown_is_not_guessed(self):
        from adapters.base import tool_type
        self.assertIn(tool_type("zzz_mystery"), ("generic", "unknown"))


class CommandTest(unittest.TestCase):
    def test_cd_prefix_is_stripped(self):
        from adapters.base import extract_cmd
        arg = {"command": 'cd "D:/Projects/App" && dotnet build 2>&1 | tail -5'}
        self.assertEqual(extract_cmd(arg), "dotnet build 2>&1 | tail -5")

    def test_powershell_cd_chain(self):
        from adapters.base import extract_cmd
        arg = {"command": 'cd /c/work ; python -m pytest -q'}
        self.assertEqual(extract_cmd(arg), "python -m pytest -q")

    def test_only_first_line_and_capped(self):
        from adapters.base import extract_cmd
        self.assertEqual(extract_cmd({"command": "make\nrm -rf /"}), "make")
        self.assertLessEqual(len(extract_cmd({"command": "a" * 5000})), 200)

    def test_cmd_class(self):
        from adapters.base import cmd_class
        self.assertEqual(cmd_class("dotnet build -c Release"), "build")
        self.assertEqual(cmd_class("python -m pytest tests/"), "test")
        self.assertEqual(cmd_class("git status"), "git")
        self.assertEqual(cmd_class("pip install requests"), "deps")
        self.assertEqual(cmd_class("ls -la"), "inspect")
        self.assertEqual(cmd_class("cat logs/a.txt | head -20"), "inspect")
        self.assertEqual(cmd_class("docker compose up -d"), "other")

    def test_inline_script_is_not_misread(self):
        """内联脚本里常含 pip/git/pytest 字样，必须归 script 而不是被那些词带走。"""
        from adapters.base import cmd_class
        for c in ('python -c "import subprocess,pytest"',
                  'python3 -X utf8 -c "import git"',
                  "python - <<'EOF'",
                  'node -e "require(\'fs\')"',
                  'cd /d/x && python -c "pass"'):
            self.assertEqual(cmd_class(c), "script", c)
        # -m 是模块入口，不是内联代码，语义仍然是测试/构建
        self.assertEqual(cmd_class("python -m pytest -q"), "test")
        self.assertEqual(cmd_class("python -m build"), "build")


class UsageTest(unittest.TestCase):
    def test_field_aliases_collapse(self):
        """三家平台的 usage 字段名各不相同，必须收敛到 in/out/cache_read。"""
        from adapters.base import usage_event
        a = usage_event({"input_tokens": 10, "output_tokens": 2,
                         "cache_read_input_tokens": 4})
        b = usage_event({"inputTokens": 10, "outputTokens": 2,
                         "cacheReadInputTokens": 4})
        c = usage_event({"prompt_tokens": 10, "completion_tokens": 2,
                         "cached_input_tokens": 4})
        for ev in (a, b, c):
            self.assertEqual(ev, {"kind": "usage", "in": 10, "out": 2, "cache_read": 4})
        self.assertIsNone(usage_event({}))
        self.assertIsNone(usage_event({"input_tokens": 0, "output_tokens": 0}))

    def test_usage_lands_on_day_and_turn(self):
        from archiver import DayCollector
        events = [
            {"kind": "ts", "value": "2026-11-19T09:00:00Z"},
            {"kind": "prompt", "text": "跑一下构建"},
            {"kind": "usage", "in": 1000, "out": 100, "cache_read": 800},
            {"kind": "ts", "value": "2026-11-20T10:00:00Z"},
            {"kind": "prompt", "text": "再补个测试"},
            {"kind": "usage", "in": 500, "out": 50, "cache_read": 0},
        ]
        col = DayCollector("C:/w", "s1", "workbuddy", "t").consume(events)
        d1 = col.days["2026-11-19"]
        d2 = col.days["2026-11-20"]
        self.assertEqual(d1["tok"], {"in": 1000, "out": 100, "cache_read": 800})
        self.assertEqual(d2["tok"], {"in": 500, "out": 50, "cache_read": 0})
        # 归属到各自的轮次，否则「一件事花多少钱」就算不出来
        self.assertEqual(d1["turns"][0]["tok"]["in"], 1000)
        self.assertEqual(d2["turns"][0]["tok"]["out"], 50)


class GitIdentityTest(unittest.TestCase):
    def test_url_forms_collapse(self):
        """同一个仓库换写法、换协议都必须得到同一身份，否则跨机器就成新项目。"""
        from adapters.base import normalize_git_url
        want = "github.com/o/r"
        for u in ("https://github.com/o/r.git",
                  "git@github.com:o/r.git",
                  "github.com/o/r",
                  "ssh://git@github.com/o/r.git"):
            self.assertEqual(normalize_git_url(u), want, u)

    def test_host_is_kept(self):
        from adapters.base import normalize_git_url
        self.assertNotEqual(normalize_git_url("git@gitlab.com:o/r.git"),
                            normalize_git_url("git@github.com:o/r.git"))


class SafeLinesTest(unittest.TestCase):
    def test_oversized_line_is_dropped_not_fatal(self):
        """一条超长行不能吃掉内存，也不能让后面的记录一起丢掉。"""
        import tempfile
        from adapters.base import MAX_LINE, safe_lines
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            f.write('{"a":1}\n')
            f.write("x" * (MAX_LINE + 10) + "\n")
            f.write('{"a":2}\n')
            p = f.name
        try:
            lines = [l.strip() for l in safe_lines(p)]
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(lines, ['{"a":1}', '{"a":2}'])


class ExternalGroupTest(unittest.TestCase):
    """工作区外写入的归属：能被看见，但不能污染统计口径。"""

    EVENTS = [
        ev("ts", value="2026-11-19T09:00:00Z"),
        ev("prompt", text="改一下插件的配置"),
        ev("tool", name="Edit"),
        ev("write", rel="src/a.cs", op="edit", content=""),
        ev("ts", value="2026-11-19T09:10:00Z"),
        ev("tool", name="Edit"),
        ev("write", rel="", external="/fake/other-repo/b.lua", op="edit"),
        ev("ts", value="2026-11-19T09:20:00Z"),
        ev("tool", name="Write"),
        ev("write", rel="", external="/fake/other-repo/b.lua", op="write"),
    ]

    def test_external_kept_out_of_workspace_files(self):
        col = DayCollector(CWD, "s1", "workbuddy", "t").consume(self.EVENTS)
        b = col.days["2026-11-19"]
        self.assertEqual(list(b["files"]), ["src/a.cs"])
        self.assertEqual(b["exts"], 2)
        self.assertEqual(len(b["extfiles"]["other-repo"]), 1)

    def test_merge_promotes_external_to_row(self):
        col = DayCollector(CWD, "s1", "workbuddy", "t").consume(self.EVENTS)
        g = {"k": {"ws": CWD, "sessions": [], "prompts": col.days["2026-11-19"]["prompts"],
                   "files": col.days["2026-11-19"]["files"], "tools": {},
                   "external": col.days["2026-11-19"]["external"],
                   "first": "", "last": "", "turns": [], "tcls": {}, "ccmd": {},
                   "tok": {"in": 0, "out": 0, "cache_read": 0},
                   "extfiles": col.days["2026-11-19"]["extfiles"],
                   "extspan": col.days["2026-11-19"]["extspan"],
                   "wcalls": 0, "whits": 0, "exts": col.days["2026-11-19"]["exts"]}}
        archiver._merge_external_groups(g)
        row = g.get("ext::other-repo")
        self.assertIsNotNone(row)
        self.assertTrue(row["is_ext"])
        self.assertEqual(row["exts"], 2)
        self.assertEqual(list(row["files"]), ["other-repo/b.lua"])
        # 区外行不能凭空分到 token：usage 没有目录维度
        self.assertEqual(sum(row["tok"].values()), 0)


class RenderMdTest(unittest.TestCase):
    def _collect(self):
        col = DayCollector(CWD, "s1", "workbuddy", "t").consume([
            ev("ts", value="2026-11-19T09:00:00Z"),
            ev("prompt", text="修掉登录框的偏移"),
            ev("tool", name="Edit"),
            ev("write", rel="src/login.xaml", op="edit", content=""),
            ev("tool", name="Bash", cmd="dotnet build"),
            ev("usage", **{"in": 1000, "out": 100, "cache_read": 800}),
        ])
        g = {"ws": CWD, "sessions": [], "prompts": col.days["2026-11-19"]["prompts"],
             "files": col.days["2026-11-19"]["files"], "tools": {}, "external": {},
             "first": "", "last": "", "turns": col.days["2026-11-19"]["turns"],
             "tcls": col.days["2026-11-19"]["tcls"], "ccmd": col.days["2026-11-19"]["ccmd"],
             "tok": col.days["2026-11-19"]["tok"], "extfiles": {}, "extspan": {},
             "wcalls": 0, "whits": 0, "exts": 0}
        g["writes"] = 0
        g["edits"] = len(g["files"])
        stat = {"hit": 1, "untimed": 0, "failed": 0, "no_usage": 0,
                "write_calls": 1, "write_hits": 1, "external": 0}
        return {"ok": True, "stat": stat, "groups": {"k": g},
                "adapters": [type("A", (), {"name": "workbuddy"})()]}

    def test_md_is_deterministic(self):
        r = self._collect()
        a = archiver.render_daylog_md("2026-11-19", r)
        b = archiver.render_daylog_md("2026-11-19", r)
        self.assertEqual(a, b)

    def test_md_carries_evidence(self):
        md = archiver.render_daylog_md("2026-11-19", self._collect())
        self.assertIn("# 工作日志 2026-11-19", md)
        self.assertIn("修掉登录框的偏移", md)
        self.assertIn("src/login.xaml", md)
        self.assertIn("构建×1", md)
        self.assertIn("未调用模型", md)

    def test_md_redacts_secret(self):
        r = self._collect()
        g = r["groups"]["k"]
        g["turns"][0]["prompt"] = "把 api_key=sk-abcdefghijklmnopqrstuvwxyz012345 换掉"
        md = archiver.render_daylog_md("2026-11-19", r)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", md)
        self.assertIn("<REDACTED>", md)


class CmdLogTest(unittest.TestCase):
    """落盘必须幂等：定时任务每天跑，无变化时不能产生 diff。"""

    def test_write_then_no_change(self):
        import tempfile
        from pathlib import Path as _P
        r = RenderMdTest()._collect()
        orig = archiver.collect_day
        archiver.collect_day = lambda *a, **k: r
        try:
            import contextlib
            import io
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
                archiver.cmd_log(_P(d), "2026-11-19", out_dir=d)
                f = _P(d) / "2026-11-19.md"
                self.assertTrue(f.exists())
                first = f.read_text(encoding="utf-8")
                archiver.cmd_log(_P(d), "2026-11-19", out_dir=d)
                self.assertEqual(f.read_text(encoding="utf-8"), first)
        finally:
            archiver.collect_day = orig


class SensitivePathTest(unittest.TestCase):
    """日志是「给人看」的出口：凭据类路径不能因为「只是路径」就放过。"""

    def test_labels(self):
        for p in (".ssh/config", "home/.aws/credentials", ".env",
                  "deploy/client.pem", "C:/Users/x/.kube/config"):
            self.assertEqual(archiver.safe_path_label(p), "（敏感路径已隐藏）", p)
        for p in ("Temp/commit-msg.txt", "repo/src/a.cs", "environment.md"):
            self.assertNotEqual(archiver.safe_path_label(p), "（敏感路径已隐藏）", p)

    def test_render_hides_external_secret(self):
        r = RenderMdTest()._collect()
        g = r["groups"]["k"]
        g["is_ext"] = False
        ext = dict(g)
        ext.update({"ws": ".ssh", "is_ext": True, "prompts": [], "turns": [],
                    "files": {"config": "write"}, "exts": 1, "first": "", "last": ""})
        r["groups"]["ext::ssh"] = ext
        md = archiver.render_daylog_md("2026-11-19", r)
        self.assertIn("敏感路径", md)
        self.assertNotIn(".ssh", md)


class TimestampContractTest(unittest.TestCase):
    def test_numeric_string_is_ms(self):
        """毫秒存成字符串时必须还原成整数，否则跨平台混排会崩。"""
        from adapters.base import iso_to_ms
        self.assertEqual(iso_to_ms("1763500000000"), 1763500000000)
        self.assertEqual(iso_to_ms(1763500000000), 1763500000000)
        self.assertEqual(iso_to_ms("2026-11-18T09:00:00Z") % 1000, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
