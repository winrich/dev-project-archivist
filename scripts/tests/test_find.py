#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""find 跨项目检索的单元测试。

守的四条语义：
    1. 多个检索词是「全部命中」而不是「任一命中」——否则结果会糊成一片
    2. 名字命中排在文件命中之前——用户记得的通常是名字
    3. 命中数量做饱和处理——上千个备份文件名不能把评分抬到天上
    4. 敏感路径既不该显示，也不该被检索命中

运行：
    python scripts/tests/test_find.py
    或 python -m unittest discover -s scripts/tests
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import cmd_find, find_tokens, parse_conversations, snippet_of  # noqa: E402


def conv_md(sessions):
    """按 build_conversations 的真实格式拼一份 conversations.md"""
    L = ["# 原始对话索引", ""]
    for i, s in enumerate(sessions, 1):
        L += [f"## {i}. {s['title']}", "", "| 项 | 值 |", "|---|---|",
              f"| 时间 | {s.get('time', '2026-11-11 19:50:00')} |",
              f"| 会话 ID | `{s.get('sid', 'sid1')}` |",
              f"| 直接打开 | [{s.get('link', '')}]({s.get('link', '')}) |", ""]
        L.append(f"**用户指令（{len(s['prompts'])} 条，按时间顺序）**")
        L.append("")
        for j, p in enumerate(s["prompts"], 1):
            first, *rest = p.split("\n")
            L.append(f"{j}. {first}")
            for line in rest:
                L.append(f"   > {line}")
        L.append("")
    return "\n".join(L)


class FindTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-find-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkproj(self, name, files=(), prompts=(), purpose="", last_dev="2026-11-10",
               workspace="C:/ws/x", session_title=None):
        d = self.tmp / f"2026-11-10_{name}"
        (d / "source").mkdir(parents=True, exist_ok=True)
        (d / "_meta").mkdir(parents=True, exist_ok=True)
        for f in files:
            p = d / "source" / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        (d / "_meta" / "conversations.md").write_text(
            conv_md([{"title": session_title or name, "prompts": list(prompts) or ["占位"]}]),
            encoding="utf-8")
        return {"name": name, "dir": d.name, "type": "代码项目", "language": "Python",
                "first_dev": "2026-11-10", "last_dev": last_dev, "sessions": 1,
                "files": len(files), "status": "active", "workspace": workspace,
                "purpose": purpose}

    def write_index(self, projects):
        (self.tmp / "index.json").write_text(
            json.dumps({"generated_at": "2026-11-18 23:00:00",
                        "total": len(projects), "projects": projects},
                       ensure_ascii=False), encoding="utf-8")

    def run_find(self, query, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_find(self.tmp, query, **kw)
        return rc, buf.getvalue()


class TestTokens(unittest.TestCase):
    def test_split(self):
        self.assertEqual(find_tokens("阅读器 插件"), ["阅读器", "插件"])

    def test_cjk_single(self):
        self.assertEqual(find_tokens("报表导出"), ["报表导出"])

    def test_empty(self):
        self.assertEqual(find_tokens("   "), [])


class TestSnippet(unittest.TestCase):
    def test_hit_position(self):
        s = snippet_of("请帮我" + "很长" * 60 + "定位这段", ["定位"], width=10)
        self.assertIn("定位", s)
        self.assertTrue(s.startswith("…"))

    def test_short_text_kept(self):
        self.assertEqual(snippet_of("写个脚本", ["脚本"]), "写个脚本")


class TestParseConversations(FindTestBase):
    def test_parses_sessions_and_multiline(self):
        d = self.tmp / "p"
        (d / "_meta").mkdir(parents=True)
        (d / "_meta" / "conversations.md").write_text(conv_md([
            {"title": "会话甲", "sid": "s1", "link": "https://x/1",
             "prompts": ["第一行\n续行内容", "第二条"]},
        ]), encoding="utf-8")
        got = parse_conversations(d / "_meta" / "conversations.md")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "会话甲")
        self.assertEqual(got[0]["sid"], "s1")
        self.assertEqual(got[0]["link"], "https://x/1")
        self.assertEqual(got[0]["prompts"][0], "第一行 续行内容")
        self.assertEqual(len(got[0]["prompts"]), 2)

    def test_missing_file(self):
        self.assertEqual(parse_conversations(self.tmp / "nope.md"), [])


class TestCmdFind(FindTestBase):
    def test_name_beats_file(self):
        p1 = self.mkproj("报表导出器", files=["a.py"])
        p2 = self.mkproj("杂项脚本", files=["报表_临时.py"])
        self.write_index([p1, p2])
        rc, out = self.run_find("报表")
        self.assertEqual(rc, 0)
        # 名字命中的必须排第一
        self.assertLess(out.index("报表导出器"), out.index("杂项脚本"))

    def test_all_tokens_required(self):
        p = self.mkproj("报表导出器", files=["a.py"], prompts=["改一下插值算法"])
        self.write_index([p])
        rc, _ = self.run_find("报表 PowerShell")
        self.assertEqual(rc, 1, "第二个词没命中，不应算命中")

    def test_tokens_can_cross_fields(self):
        # 「报表」在名字里、「插值」在指令里，合起来应命中
        p = self.mkproj("报表导出器", files=["a.py"], prompts=["把插值换成 LANCZOS4"])
        self.write_index([p])
        rc, out = self.run_find("报表 插值")
        self.assertEqual(rc, 0)
        self.assertIn("[指令]", out)

    def test_saturation_keeps_scores_sane(self):
        many = [f"backup/f{i:04d}_reader.txt" for i in range(300)]
        p1 = self.mkproj("甲项目", files=many)
        p2 = self.mkproj("乙项目", files=["reader.py"])
        self.write_index([p1, p2])
        rc, out = self.run_find("reader", as_json=True)
        data = json.loads(out)
        scores = {h["name"]: h["score"] for h in data["hits"]}
        # 300 个文件与 1 个文件的评分差必须被压住（饱和上限 6）
        self.assertLessEqual(scores["甲项目"] - scores["乙项目"], archiver.FIND_CAP)
        self.assertLess(max(scores.values()), 40)

    def test_summary_only_is_weak(self):
        p = self.mkproj("无关项目", files=["a.py"], purpose="用于处理报表导出")
        self.write_index([p])
        rc, out = self.run_find("报表")
        self.assertIn("弱命中", out)
        rc2, out2 = self.run_find("报表", show_all=True)
        self.assertIn("无关项目", out2)

    def test_sensitive_path_hidden(self):
        p = self.mkproj("某项目", files=["config.py", ".ssh/config"])
        self.write_index([p])
        rc, out = self.run_find("ssh")
        self.assertEqual(rc, 1, "敏感路径不应被检索命中")
        self.assertNotIn(".ssh", out)

    def test_no_index(self):
        rc, out = self.run_find("任意")
        self.assertEqual(rc, 1)
        self.assertIn("没有索引", out)

    def test_empty_query(self):
        self.write_index([])
        rc, out = self.run_find("  ")
        self.assertEqual(rc, 1)
        self.assertIn("用法", out)

    def test_json_shape(self):
        p = self.mkproj("报表导出器", files=["a.py"], prompts=["处理报表导出"])
        self.write_index([p])
        rc, out = self.run_find("报表", as_json=True)
        data = json.loads(out)
        self.assertEqual(data["query"], "报表")
        self.assertEqual(data["total"], 1)
        h = data["hits"][0]
        for k in ("name", "dir", "workspace", "score", "snippets"):
            self.assertIn(k, h)

    def test_recent_first_on_tie(self):
        a = self.mkproj("项目甲", files=["z.py"], last_dev="2026-11-01")
        b = self.mkproj("项目乙", files=["z.py"], last_dev="2026-11-15")
        self.write_index([a, b])
        rc, out = self.run_find("z.py")
        self.assertLess(out.index("项目乙"), out.index("项目甲"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
