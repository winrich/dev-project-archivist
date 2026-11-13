#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器单元测试。

本机若没有安装某个平台的工具（例如 Claude Code），真实数据无从获得，
因此这里用「按公开格式规范构造的样本」验证适配器逻辑是否正确。
样本覆盖了已知的格式陷阱：content 恒为数组、tool_result 内容可为字符串或数组、
isSidechain 子代理记录、isMeta 记录、中断标记、ISO 时间戳。

运行：
    python scripts/tests/test_adapters.py
    或 python -m unittest discover -s scripts/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from adapters import MultiIndex  # noqa: E402
from adapters.claude_code import ClaudeCodeAdapter, ClaudeCodeIndex  # noqa: E402

CWD = "/work/demo-app"

# 一段贴近真实结构的 Claude Code 会话：含需求、思考块、写文件、工具结果、
# 子代理记录、isMeta 记录、中断标记、标题记录、错误输出。
CC_SAMPLE = [
    {"type": "user", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:00.000Z", "isSidechain": False,
     "message": {"role": "user", "content": [
         {"type": "text", "text": "<system-reminder>ignore me</system-reminder>"
                                  "帮我做一个计时工具，记录每个函数的耗时"}]}},

    {"type": "assistant", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:05.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "内部推理，不该出现在结果里"},
         {"type": "text", "text": "好的，我先建项目结构。"}]}},

    {"type": "assistant", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:06.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "t1", "name": "Write",
          "input": {"file_path": CWD + "/src/Timer.cs", "content": "class Timer { }"}}]}},

    {"type": "assistant", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:07.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "t2", "name": "Write",
          "input": {"file_path": "/other/place/Ignored.cs", "content": "不该被记录"}}]}},

    {"type": "user", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:08.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "t1", "content": "文件已写入"}]}},

    {"type": "user", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:09.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "t3",
          "content": [{"type": "text", "text": "编译失败：CS1002 需要 ;"}]}]}},

    {"type": "user", "sessionId": "sess-1", "cwd": CWD, "isSidechain": True,
     "timestamp": "2026-11-01T10:00:10.000Z",
     "message": {"role": "user", "content": [
         {"type": "text", "text": "子代理的指令，不该算用户提问"}]}},

    {"type": "user", "sessionId": "sess-1", "cwd": CWD, "isMeta": True,
     "timestamp": "2026-11-01T10:00:11.000Z",
     "message": {"role": "user", "content": [
         {"type": "text", "text": "元信息记录，不该算用户提问"}]}},

    {"type": "user", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:00:12.000Z",
     "message": {"role": "user", "content": [
         {"type": "text", "text": "[Request interrupted by user]"}]}},

    {"type": "custom-title", "sessionId": "sess-1", "cwd": CWD,
     "timestamp": "2026-11-01T10:05:00.000Z", "title": "耗时统计工具"},
]


class ClaudeCodeAdapterTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "sess-1.jsonl"
        with open(self.file, "w", encoding="utf-8") as f:
            for o in CC_SAMPLE:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
        self.adapter = ClaudeCodeAdapter()
        self.parser = archiver.SessionParser(self.file, CWD).parse(self.adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_only_real_user_text(self):
        """只保留真实提问：注入块被剥离，子代理/isMeta/中断标记全部排除。"""
        self.assertEqual(self.parser.user_prompts,
                         ["帮我做一个计时工具，记录每个函数的耗时"])
        self.assertEqual(self.parser.turns, 1)

    def test_assistant_text_and_tool_count(self):
        self.assertEqual(self.parser.assistant_chars, len("好的，我先建项目结构。"))
        self.assertEqual(self.parser.tool_counts.get("Write"), 2)

    def test_write_event_relative_and_scoped(self):
        """工作区内的写入被相对化，工作区外的一律丢弃。"""
        self.assertEqual(list(self.parser.written), ["src/Timer.cs"])
        self.assertEqual(self.parser.written["src/Timer.cs"], "class Timer { }")

    def test_tool_result_errors_both_shapes(self):
        """tool_result 的 content 支持字符串与数组两种形态，且只留错误线索。"""
        joined = " | ".join(self.parser.errors)
        self.assertIn("CS1002", joined)
        self.assertNotIn("文件已写入", joined)

    def test_title_from_custom_title(self):
        self.assertEqual(self.parser.titles, ["耗时统计工具"])

    def test_timestamp_parsed(self):
        self.assertTrue(self.parser.first_ts and self.parser.last_ts)
        self.assertLess(self.parser.first_ts, self.parser.last_ts)


class ClaudeCodeIndexTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        proj = self.root / "-work-demo-app"
        proj.mkdir(parents=True)
        with open(proj / "sess-1.jsonl", "w", encoding="utf-8") as f:
            for o in CC_SAMPLE:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
        # 子代理目录里的文件不应被当作独立会话
        sub = proj / "sess-1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")

        import adapters.claude_code as cc
        self._orig = cc.PROJECTS_DIR
        cc.PROJECTS_DIR = self.root

    def tearDown(self):
        import adapters.claude_code as cc
        cc.PROJECTS_DIR = self._orig
        self.tmp.cleanup()

    def test_index_reads_cwd_and_time(self):
        idx = ClaudeCodeIndex().load()
        self.assertEqual(list(idx.ids()), ["sess-1"])
        info = idx.info("sess-1")
        self.assertEqual(info["cwd"], CWD)
        self.assertEqual(info["source"], "claude-code")
        # 时间必须是毫秒整数，否则与 WorkBuddy 混排时 min/max 会抛 TypeError
        self.assertIsInstance(info["created"], int)
        self.assertIsInstance(info["updated"], int)
        self.assertEqual(info["title"], "耗时统计工具")
        self.assertIsNotNone(idx.transcript("sess-1"))


class MultiIndexTest(unittest.TestCase):

    def test_aggregate_and_resolve_conflict(self):
        class FakeIndex:
            def __init__(self, data):
                self.data = data

            def ids(self):
                return set(self.data)

            def info(self, sid):
                return self.data.get(sid, {})

            def transcript(self, sid):
                return f"/tmp/{sid}.jsonl"

        a = FakeIndex({"dup": {"cwd": "/a"}, "only-a": {"cwd": "/a2"}})
        b = FakeIndex({"dup": {"cwd": "/b"}})
        mix = MultiIndex([("alpha", a), ("beta", b)])

        self.assertEqual(len(mix.ids()), 3)
        self.assertEqual(mix.info("dup")["source"], "alpha")
        self.assertEqual(mix.info("beta:dup")["source"], "beta")
        self.assertEqual(mix.transcript("beta:dup"), "/tmp/dup.jsonl")
        self.assertEqual(mix.transcript("only-a"), "/tmp/only-a.jsonl")
        self.assertIsNone(mix.transcript("不存在"))


class AdapterDispatchTest(unittest.TestCase):
    """深链与提示文案必须按来源分派，不能把 A 平台的链接塞给 B 平台。"""

    def test_deep_link_by_source(self):
        self.assertEqual(archiver.deep_link_of("abc", "workbuddy"), "workbuddy://chat/abc")
        self.assertEqual(archiver.deep_link_of("abc", "claude-code"), "")
        self.assertEqual(archiver.deep_link_of("", "workbuddy"), "")

    def test_open_hint_by_source(self):
        self.assertIn("workbuddy://", archiver.open_hint_of("workbuddy"))
        self.assertIn("claude --resume", archiver.open_hint_of("claude-code"))

    def test_unknown_source_falls_back(self):
        self.assertTrue(archiver.deep_link_of("abc", "不存在的平台"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
