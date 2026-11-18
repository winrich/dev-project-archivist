#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按日恢复 `restore` 的单元测试。

守的四条（比"功能能用"更重要）：
    1. **语义**：取的是「那天结束时归档里最后一次提交」，不是最后一次提交，
       也不是那天改了什么。日期边界按本地时间算，不按 UTC 算。
    2. **不可恢复要能解释**：首次归档之前、空仓库，都必须给出理由，
       而不是静默少一行。
    3. **绝不写回归档**：恢复只读归档仓库，产物落在另一处；跑完归档仓库
       仍然 `git status` 干净，工作区文件一个字节都没变。
    4. **默认只预览**：不加 --write 时磁盘上不能多出任何东西——
       恢复动辄几百 MB，"手滑"的代价太高。

运行：
    python scripts/tests/test_restore.py
    或 python -m unittest discover -s scripts/tests
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    RESTORE_DIR, build_restore_index, cmd_restore, git_commit_at, git_export,
    git_tree_files, restore_dates,
)

GIT = shutil.which("git")


def ts_of(s: str) -> int:
    """本地时区的 Unix 秒，和实现里的口径一致。"""
    return int(datetime.strptime(s, "%Y-%m-%d").timestamp())


def end_of_day(date_str: str) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, 23, 59, 59).timestamp())


def run_git(adir: Path, *args, date: str = "", env_extra: dict = None):
    env = dict(os.environ)
    env.update({"GIT_CONFIG_NOSYSTEM": "1"})
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["git"] + list(args), cwd=str(adir), env=env,
                          capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")


def make_repo(root: Path, name: str, commits: list) -> Path:
    """建一个归档器风格的仓库。

    commits: [(提交日期 "2026-11-14T22:00:00+0800", {相对路径: 内容}, 提交信息)]
    日期带偏移量是有意的：真实归档器的提交带本机时区，测试必须能撞上
    "UTC 看是前一天"的边界。
    """
    adir = root / name
    adir.mkdir(parents=True, exist_ok=True)
    run_git(adir, "init", "-q")
    run_git(adir, "config", "core.autocrlf", "false")
    for date, files, msg in commits:
        for rel, text in files.items():
            p = adir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        run_git(adir, "add", "-A")
        run_git(adir, "-c", "user.name=archiver", "-c", "user.email=archiver@local",
                "commit", "-q", "-m", msg, date=date)
    return adir


def quiet(fn, *a, **kw):
    """吞掉命令输出，只留返回值。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


@unittest.skipIf(not GIT, "本机没有 git，跳过恢复相关测试")
class RestoreBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "归档"
        self.root.mkdir()
        # ProjA：两天有提交，能看到"取的是当日最后一次"这条语义
        make_repo(self.root, "ProjA", [
            ("2026-11-10T10:00:00+0800",
             {"README.md": "# A\n", "src/a.py": "a\n"}, "init: 归档 ProjA"),
            ("2026-11-14T22:00:00+0800",
             {"src/a.py": "a2\n", "src/b.py": "b\n"}, "sync: 加了 b.py"),
        ])
        # ProjB：11-16 才开始归档，用来验证"首次归档之前恢复不了"有解释
        make_repo(self.root, "ProjB", [
            ("2026-11-16T09:00:00+0800", {"x.md": "x\n"}, "init: 归档 ProjB"),
        ])

    def tearDown(self):
        self._td.cleanup()


class TestCommitAt(RestoreBase):
    def test_takes_last_commit_of_that_day(self):
        """11-14 要拿到 11-14 那次，而不是 11-10 的首次提交。"""
        c = git_commit_at(self.root / "ProjA", end_of_day("2026-11-14"))
        self.assertTrue(c["sha"])
        self.assertEqual(c["subject"], "sync: 加了 b.py")

    def test_between_commits_falls_back_to_earlier(self):
        """11-12 那天没跑归档 → 拿到 11-10 的快照，且带真实提交时间。

        这里最容易错的做法是"精确匹配当天提交，没有就报错"，
        结果是用户在没跑归档的日子上什么都恢复不了。
        """
        c = git_commit_at(self.root / "ProjA", end_of_day("2026-11-12"))
        self.assertEqual(c["subject"], "init: 归档 ProjA")
        self.assertEqual(datetime.fromtimestamp(c["ts"]).strftime("%Y-%m-%d"),
                         "2026-11-10")

    def test_before_first_commit_reports_first_ts(self):
        """首次归档之前：没有 sha，但要有 first_ts，才能说清"首次归档是哪天"。"""
        c = git_commit_at(self.root / "ProjA", end_of_day("2026-11-01"))
        self.assertEqual(c["sha"], "")
        self.assertEqual(datetime.fromtimestamp(c["first_ts"]).strftime("%Y-%m-%d"),
                         "2026-11-10")

    def test_boundary_uses_local_day_not_utc(self):
        """00:30 +0800 的提交在 UTC 是前一天 16:30。

        按 UTC 日期比较会把 11-14 凌晨的提交判到 11-13，用户回到 11-14 就看不到它。
        """
        adir = make_repo(self.root, "ProjC", [
            ("2026-11-14T00:30:00+0800", {"late.txt": "x\n"}, "init: 归档 ProjC"),
        ])
        c = git_commit_at(adir, end_of_day("2026-11-14"))
        self.assertTrue(c["sha"], "本地时间 11-14 的提交必须能恢复")
        # 而按 UTC 日界（= 11-13 16:30Z 之后）的 11-13 结束时刻应当拿不到
        c13 = git_commit_at(adir, end_of_day("2026-11-13"))
        self.assertEqual(c13["sha"], "")

    def test_empty_repo_is_not_an_error(self):
        """git init 了但没提交：不抛异常，只是没有可恢复内容。

        返回空 dict 而不是 {"sha": ""}：调用方判 `not c` 就走「仓库里没有提交」，
        这条分支必须和"有提交但都在这天之后"区分开，提示语不一样。
        """
        adir = self.root / "Empty"
        adir.mkdir()
        run_git(adir, "init", "-q")
        c = git_commit_at(adir, end_of_day("2026-11-18"))
        self.assertFalse(c.get("sha"))
        self.assertFalse(c.get("first_ts"))

    def test_no_git_dir(self):
        self.assertEqual(git_commit_at(self.root / "NotARepo", 10 ** 10), {})


class TestTreeAndExport(RestoreBase):
    def test_tree_files_reads_sizes_without_unpacking(self):
        """预览要零成本：只读树对象就能报出文件数与体积。"""
        sha = git_commit_at(self.root / "ProjA", end_of_day("2026-11-14"))["sha"]
        files = dict(git_tree_files(self.root / "ProjA", sha))
        self.assertIn("src/b.py", files)
        self.assertNotIn("src/b.py", dict(git_tree_files(
            self.root / "ProjA",
            git_commit_at(self.root / "ProjA", end_of_day("2026-11-10"))["sha"])))

    def test_export_writes_old_content_to_new_dir(self):
        adir = self.root / "ProjA"
        sha = git_commit_at(adir, end_of_day("2026-11-10"))["sha"]
        dest = Path(self._td.name) / "out" / "ProjA"
        res = git_export(adir, sha, dest)
        self.assertTrue(res["ok"])
        self.assertEqual((dest / "src" / "a.py").read_text(encoding="utf-8"), "a\n")
        self.assertFalse((dest / "src" / "b.py").exists(), "不能出现那天的未来文件")

    def test_export_does_not_carry_git_dir(self):
        """副本是给人拿去看/编译的，不该自带一个指向别处的 .git。"""
        adir = self.root / "ProjA"
        sha = git_commit_at(adir, end_of_day("2026-11-14"))["sha"]
        dest = Path(self._td.name) / "out2" / "ProjA"
        git_export(adir, sha, dest)
        self.assertFalse((dest / ".git").exists())


class TestCmdRestore(RestoreBase):
    def test_no_date_lists_dates(self):
        """不带 --date：列出可恢复日期，而不是报参数错误。"""
        rc, out = quiet(cmd_restore, self.root)
        self.assertEqual(rc, 1)
        self.assertIn("2026-11-14", out)
        self.assertIn("2026-11-10", out)
        self.assertIn("--write", out)

    def test_preview_writes_nothing(self):
        """默认只预览：磁盘上不能多出 _restore。"""
        rc, out = quiet(cmd_restore, self.root, "2026-11-14")
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / RESTORE_DIR).exists())
        self.assertIn("预览", out)

    def test_write_creates_copy_and_leaves_archive_clean(self):
        before = (self.root / "ProjA" / "src" / "a.py").read_text(encoding="utf-8")
        rc, out = quiet(cmd_restore, self.root, "2026-11-14", write=True)
        self.assertEqual(rc, 0)
        dest = self.root / RESTORE_DIR / "2026-11-14" / "ProjA"
        self.assertTrue((dest / "src" / "b.py").exists())
        self.assertEqual((self.root / "ProjA" / "src" / "a.py").read_text(encoding="utf-8"),
                         before)
        st = run_git(self.root / "ProjA", "status", "--porcelain")
        self.assertEqual((st.stdout or "").strip(), "", "归档仓库必须保持干净")

    def test_skipped_project_explained(self):
        """ProjB 在 11-14 还没归档 → 要说明首次归档日期。"""
        rc, out = quiet(cmd_restore, self.root, "2026-11-14")
        self.assertIn("ProjB", out)
        self.assertIn("2026-11-16", out)

    def test_date_before_everything_returns_error_and_no_dir(self):
        rc, out = quiet(cmd_restore, self.root, "2026-11-01", write=True)
        self.assertEqual(rc, 1)
        self.assertFalse((self.root / RESTORE_DIR).exists())

    def test_only_filters(self):
        rc, out = quiet(cmd_restore, self.root, "2026-11-16", only="projb")
        self.assertEqual(rc, 0)
        self.assertIn("ProjB", out)
        self.assertNotIn("ProjA", out)

    def test_only_no_match_is_explained(self):
        rc, out = quiet(cmd_restore, self.root, "2026-11-16", only="zzz")
        self.assertEqual(rc, 1)
        self.assertIn("没有项目匹配", out)

    def test_second_write_refused_without_force(self):
        """同一天再写一次：默认拒绝，避免把手工改过的副本又盖回去。"""
        quiet(cmd_restore, self.root, "2026-11-14", write=True)
        rc, out = quiet(cmd_restore, self.root, "2026-11-14", write=True)
        self.assertEqual(rc, 2)
        self.assertIn("已有内容", out)
        rc2, _ = quiet(cmd_restore, self.root, "2026-11-14", write=True, force=True)
        self.assertEqual(rc2, 0)

    def test_out_dir_honoured(self):
        alt = Path(self._td.name) / "另一个地方"
        rc, _ = quiet(cmd_restore, self.root, "2026-11-14", write=True, out=str(alt))
        self.assertEqual(rc, 0)
        self.assertTrue((alt / "2026-11-14" / "ProjA" / "README.md").exists())

    def test_index_written(self):
        quiet(cmd_restore, self.root, "2026-11-14", write=True)
        d = self.root / RESTORE_DIR / "2026-11-14"
        md = (d / "_restore.md").read_text(encoding="utf-8")
        self.assertIn("不是「那天改了什么」", md)
        self.assertIn("ProjA", md)
        data = json.loads((d / "_restore.json").read_text(encoding="utf-8"))
        self.assertEqual(data["projects"][0]["dir"], "ProjA")

    def test_bad_date(self):
        rc, out = quiet(cmd_restore, self.root, "2026-13-99")
        self.assertEqual(rc, 1)
        self.assertIn("YYYY-MM-DD", out)

    def test_empty_target(self):
        empty = Path(self._td.name) / "空的"
        empty.mkdir()
        rc, out = quiet(cmd_restore, empty, "2026-11-14")
        self.assertEqual(rc, 1)
        self.assertIn("没有", out)

    def test_json_preview(self):
        quiet(cmd_restore, self.root, "2026-11-14")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_restore(self.root, "2026-11-14", as_json=True)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["preview"])
        self.assertEqual(data["restored"][0]["dir"], "ProjA")
        self.assertEqual(data["skipped"][0]["dir"], "ProjB")


class TestRestoreDates(RestoreBase):
    def test_dates_desc_and_counted(self):
        rows = restore_dates(self.root)
        dates = [r[0] for r in rows]
        self.assertEqual(dates, ["2026-11-16", "2026-11-14", "2026-11-10"])
        self.assertEqual(rows[0][1], 1)          # 11-16 只有 ProjB

    def test_underscore_dirs_ignored(self):
        """_restore / _meta 不是项目：恢复时不能被当成仓库。"""
        (self.root / "_docs").mkdir()
        self.assertNotIn("_docs", [p.name for p in archiver.archive_dirs(self.root)])


class TestIndexText(unittest.TestCase):
    def test_index_states_semantics(self):
        md = build_restore_index("2026-11-14",
                                 [{"dir": "A", "when": "2026-11-14 22:00",
                                   "sha": "0123456789", "subject": "s",
                                   "files": 2, "bytes": 10}],
                                 [{"dir": "B", "reason": "首次归档 2026-11-16"}],
                                 [("C", "解包失败")])
        self.assertIn("2026-11-14 23:59:59", md)
        self.assertIn("不是「那天改了什么」", md)
        self.assertIn("**B** — 首次归档 2026-11-16", md)
        self.assertIn("**C** — 解包失败", md)
        self.assertIn("01234567", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
