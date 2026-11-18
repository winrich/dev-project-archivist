#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交信息生成的单元测试。

守的一条底线：**每天的提交得说明白那天干了什么**，但不能因此泄密或编造。

改动前的现场是 27 个仓库每天各写一行 `sync: 同步 X 归档内容`——账本一直在记，
记的却只是"今天有变化"。这里把踩过的坑固化成用例：
    · token 先截断后脱敏 → 半截 token 正好躲过密钥正则
    · 多行指令里贴的代码进了 commit → `git log` 变噪声
    · 指令里提到 .ssh/config → 路径本身即信息，整条丢弃
    · 没有可用指令时 → 退回原文案，不许编

运行：
    python scripts/tests/test_commit_msg.py
    或 python -m unittest discover -s scripts/tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    ARCHIVER_TRAILER, COMMIT_ITEM_LEN, _force_rmtree, archiver_commits_only,
    commit_item, git_init, git_last_commit_ts, recent_prompts, sync_subject,
)

HAS_GIT = shutil.which("git") is not None


class FakeProject:
    """只带 recent_prompts 需要的两个字段。"""

    def __init__(self, sessions):
        self.sessions = sessions


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _msg(adir: Path) -> str:
    """最后一次提交的完整信息（subject + body）。"""
    return _git(["log", "-1", "--format=%B"], adir).stdout.strip()


class TestCommitItem(unittest.TestCase):
    def test_redacts_token_before_truncating(self):
        """脱敏必须发生在截断之前，否则半截 token 会溜过去。"""
        out = commit_item("用 sk-abcdefghijklmnopQRST 调一下接口")
        self.assertNotIn("sk-abcdef", out)
        self.assertIn("<REDACTED>", out)

    def test_token_that_would_be_cut_still_redacted(self):
        """token 横跨截断边界时也不能漏。"""
        pad = "改" * 36
        out = commit_item(pad + " sk-abcdefghijklmnopQRST")
        self.assertNotIn("sk-abc", out)

    def test_single_line_only(self):
        self.assertEqual(commit_item("第一行\n第二行\n第三行"), "第一行")

    def test_truncates_with_ellipsis(self):
        out = commit_item("修" * 80)
        self.assertLessEqual(len(out), COMMIT_ITEM_LEN + 1)
        self.assertTrue(out.endswith("…"))

    def test_sensitive_path_dropped_whole(self):
        """提到敏感路径的指令整条不要：语义替换后只剩「改了（已隐藏）」。"""
        self.assertEqual(commit_item("把 .ssh/config 的权限改一下"), "")
        self.assertEqual(commit_item("读一下 ~/.aws/credentials"), "")

    def test_short_noise_dropped(self):
        self.assertEqual(commit_item("好"), "")
        self.assertEqual(commit_item("继续"), "")

    def test_strips_leading_markers(self):
        self.assertEqual(commit_item("# 修复登录失败"), "修复登录失败")

    def test_trailing_colon_trimmed(self):
        """「你分析有误：」是引子，冒号留着像断了一半；中间冒号要保留。"""
        self.assertEqual(commit_item("你分析有误："), "你分析有误")
        self.assertEqual(commit_item("报错：EACCES"), "报错：EACCES")

    def test_windows_path_not_treated_as_sensitive(self):
        out = commit_item(r"改一下 D:\proj\src\main.cpp 的初始化")
        self.assertNotEqual(out, "")


class TestSyncSubject(unittest.TestCase):
    def test_substantive_beats_latest_followup(self):
        """真实数据里最新的一条常是「还是不行」，主题要挑有信息量的那条。"""
        head, more = sync_subject("X", [
            "新增范围配置表 config_scope 并做四级联动",
            "还是不行",
            "继续未完成的工作",
        ])
        self.assertTrue(head.startswith("新增范围配置表 config_scope"))
        self.assertIn("等 3 项", head)
        self.assertIn("还是不行", more)

    def test_tie_prefers_newest(self):
        head, _ = sync_subject("X", ["旧的活", "新的活"])
        self.assertEqual(head, "新的活 等 2 项")

    def test_dedupes_repeated_prompts(self):
        head, more = sync_subject("X", ["同一句", "同一句"])
        self.assertEqual(head, "同一句")
        self.assertEqual(more, [])

    def test_caps_at_five_items(self):
        head, more = sync_subject("X", [f"第{i}件事" for i in range(9)])
        self.assertEqual(len(more) + 1, 5)
        self.assertIn("等 5 项", head)

    def test_empty_when_nothing_usable(self):
        self.assertEqual(sync_subject("X", []), ("", []))
        self.assertEqual(sync_subject("X", ["好", "嗯"]), ("", []))

    def test_code_paste_loses_to_task(self):
        head, _ = sync_subject("X", [
            "private void Draw(Rectangle r) { g.Fill(r); }",
            "修复导出 PDF 时表格边框丢失",
        ])
        self.assertTrue(head.startswith("修复导出 PDF"))


class TestCommitItemDropsPlatformNoise(unittest.TestCase):
    def test_slash_command_dropped(self):
        self.assertEqual(commit_item("/clear"), "")
        self.assertEqual(commit_item("/compact"), "")

    def test_bare_path_dropped(self):
        self.assertEqual(commit_item("F:\\"), "")
        self.assertEqual(commit_item("https://git.example.com/team/BatchRenamer"), "")

    def test_skill_marker_stripped(self):
        out = commit_item("继续做导出功能 @skill:ardot-design-assistant")
        self.assertEqual(out, "继续做导出功能")

    def test_skill_marker_only_dropped(self):
        self.assertEqual(commit_item("@skill:ardot-design-assistant"), "")
        # 剥掉后只剩 "进度" 两个字，仍属噪声
        self.assertEqual(commit_item("进度 @skill:ardot-design-assistant"), "")


class TestRecentPrompts(unittest.TestCase):
    def test_filters_by_last_commit(self):
        p = FakeProject([
            {"sid": "old", "created": 1000, "updated": 1000},
            {"sid": "new", "created": 5000, "updated": 5000},
        ])
        parses = {"old": {"prompts": ["旧的活"], "last_ts": 1000},
                  "new": {"prompts": ["新的活"], "last_ts": 5000}}
        self.assertEqual(recent_prompts(p, parses, 2000), ["新的活"])
        self.assertEqual(recent_prompts(p, parses, 0), ["旧的活", "新的活"])

    def test_session_without_time_is_kept(self):
        """时间缺失时宁可多带一条，也不能因为读不到时间就静默丢内容。"""
        p = FakeProject([{"sid": "s", "created": None, "updated": None}])
        parses = {"s": {"prompts": ["某件事"], "last_ts": 0}}
        self.assertEqual(recent_prompts(p, parses, 9999), ["某件事"])


@unittest.skipUnless(HAS_GIT, "需要 git")
class TestGitInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-cm-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.adir = self.tmp / "proj"
        self.adir.mkdir()
        (self.adir / "a.txt").write_text("v1", encoding="utf-8")

    def _count(self):
        out = _git(["rev-list", "--count", "HEAD"], self.adir).stdout.strip()
        return int(out or 0)

    def test_init_message_has_trailer(self):
        self.assertEqual(git_init(self.adir, "示例项目"), "ok")
        msg = _msg(self.adir)
        self.assertTrue(msg.startswith("init: 归档 示例项目"))
        self.assertIn(ARCHIVER_TRAILER, msg)

    def test_sync_message_carries_prompts(self):
        git_init(self.adir, "示例项目")
        (self.adir / "a.txt").write_text("v2", encoding="utf-8")
        rc = git_init(self.adir, "示例项目",
                      prompts=["修复导入菜单无法打开窗体", "新增范围配置表 config_scope"])
        self.assertEqual(rc, "committed")
        msg = _msg(self.adir)
        subject = msg.splitlines()[0]
        self.assertEqual(subject, "sync: 新增范围配置表 config_scope 等 2 项")
        self.assertIn("- 修复导入菜单无法打开窗体", msg)
        self.assertIn(ARCHIVER_TRAILER, msg)

    def test_no_change_makes_no_commit(self):
        git_init(self.adir, "示例项目")
        before = self._count()
        self.assertEqual(git_init(self.adir, "示例项目", prompts=["随便什么"]), "exists")
        self.assertEqual(self._count(), before)

    def test_falls_back_when_no_prompt(self):
        """没有指令可用时不编造，用文件变化数兜底。"""
        git_init(self.adir, "示例项目")
        (self.adir / "b.txt").write_text("x", encoding="utf-8")
        git_init(self.adir, "示例项目", prompts=[])
        self.assertTrue(_msg(self.adir).splitlines()[0].startswith("sync: 同步 示例项目 归档内容"))

    def test_note_overrides_subject(self):
        git_init(self.adir, "示例项目")
        (self.adir / "a.txt").write_text("v3", encoding="utf-8")
        git_init(self.adir, "示例项目", note="更新 README 与索引")
        self.assertEqual(_msg(self.adir).splitlines()[0], "sync: 更新 README 与索引")

    def test_commit_time_readable(self):
        git_init(self.adir, "示例项目")
        self.assertGreater(git_last_commit_ts(self.adir), 0)
        self.assertEqual(git_last_commit_ts(self.tmp / "nope"), 0)


@unittest.skipUnless(HAS_GIT, "需要 git")
class TestArchiverCommitsOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-gc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.adir = self.tmp / "proj"
        self.adir.mkdir()
        (self.adir / "a.txt").write_text("v1", encoding="utf-8")
        git_init(self.adir, "示例项目")

    def _human_commit(self, msg):
        _git(["add", "-A"], self.adir)
        _git(["-c", "user.name=someone", "-c", "user.email=s@x",
              "commit", "-q", "-m", msg], self.adir)

    def test_archiver_only(self):
        self.assertTrue(archiver_commits_only(self.adir))

    def test_human_commit_blocks_rebuild(self):
        (self.adir / "a.txt").write_text("v2", encoding="utf-8")
        self._human_commit("我自己改的")
        self.assertFalse(archiver_commits_only(self.adir))

    def test_legacy_message_still_recognised(self):
        """v0.1.x 的提交没有 trailer，不能因为升级就把老仓库判成\"含人工提交\"。"""
        (self.adir / "a.txt").write_text("v3", encoding="utf-8")
        self._human_commit("sync: 同步 示例项目 归档内容")
        self.assertTrue(archiver_commits_only(self.adir))

    def test_empty_repo_is_safe(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        _git(["init", "-q"], empty)
        self.assertTrue(archiver_commits_only(empty))


class TestForceRmtree(unittest.TestCase):
    def test_removes_readonly_files(self):
        """Windows 上 git 把对象文件标成只读，普通 rmtree 会 WinError 5。

        这条不修，`git-clean` 在 Windows 上一个仓库都重建不了——而 Windows 正是
        这个工具最主要的使用场景。
        """
        tmp = Path(tempfile.mkdtemp(prefix="dpa-rm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        objs = tmp / ".git" / "objects" / "00"
        objs.mkdir(parents=True)
        f = objs / "abc123"
        f.write_text("x", encoding="utf-8")
        os.chmod(str(f), 0o400)          # 设为只读
        _force_rmtree(tmp / ".git")
        self.assertFalse((tmp / ".git").exists())


if __name__ == "__main__":
    unittest.main()
