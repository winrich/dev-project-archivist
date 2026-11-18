#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档仓库同步 `sync` 的单元测试。

守的四条（比"能推上去"更重要）：
    1. **只推配了远端的**。归档器不替用户决定备份到哪儿；没配就跳过并给出配置方法，
       绝不自动建远端——那等于把源码推去一个没人选过的地方。
    2. **永不 force**。远端有本地没有的提交时只报告，远端一个字节都不能变。
    3. **默认只预览**。不加 --push 时不能多出任何提交——同步会写 git 历史，
       手滑的代价是往用户自己的仓库里灌垃圾提交。
    4. **不能不吭声**。空仓库、分离头指针、认证失败都必须出现在结果里，
       静默跳过会让人以为已经推上去了。

所有用例都在本机临时目录里建 bare 仓库当远端，不联网、不碰真实归档。

运行：
    python scripts/tests/test_sync.py
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    ARCHIVER_TRAILER, _short_url, cmd_sync, git_remote_names, repo_sync_state,
)

GIT = shutil.which("git")


def run_git(cwd: Path, *args, check: bool = True):
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run([GIT, "-c", "core.autocrlf=false"] + list(args),
                       cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{r.stderr}")
    return r


def git_out(cwd: Path, *args) -> str:
    return (run_git(cwd, *args).stdout or "").strip()


@unittest.skipIf(GIT is None, "本机没有 git")
class SyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.arc = self.root / "arc"
        self.arc.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 夹具
    def bare(self, name: str) -> Path:
        b = self.root / "remotes" / f"{name}.git"
        b.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.root, "init", "-q", "--bare", str(b))
        return b

    def repo(self, name: str, remote: Path = None, commit: bool = True) -> Path:
        d = self.arc / name
        d.mkdir(parents=True, exist_ok=True)
        run_git(d, "init", "-q", "-b", "main")
        if commit:
            (d / "a.py").write_text("a\n", encoding="utf-8")
            run_git(d, "add", "-A")
            run_git(d, "commit", "-q", "-m", f"init: 归档 {name}")
        if remote is not None:
            run_git(d, "remote", "add", "origin", str(remote))
        return d

    def sync(self, *args, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_sync(self.arc, *args, **kw)
        return rc, buf.getvalue()

    def remote_commits(self, bare: Path) -> list:
        """远端已有的提交标题。空 bare 仓库没有 HEAD，`git log` 会失败，按空列表算。"""
        out = (run_git(bare, "log", "--format=%s", check=False).stdout or "").strip()
        return [x for x in out.splitlines() if x.strip()]


class Preview(SyncBase):
    def test_preview_writes_nothing(self):
        """不加 --push 就只打印：不提交、不推送，远端一个提交都不多。"""
        b = self.bare("A")
        d = self.repo("ProjA", remote=b)
        self.sync(push=True)                       # 先推一次，建立基准
        (d / "a.py").write_text("changed\n", encoding="utf-8")
        before = git_out(d, "rev-list", "--count", "HEAD")

        rc, out = self.sync()
        self.assertEqual(rc, 0)
        self.assertIn("预览", out)
        self.assertEqual(git_out(d, "rev-list", "--count", "HEAD"), before)
        self.assertEqual(self.remote_commits(b), ["init: 归档 ProjA"])

    def test_preview_counts_pending_changes(self):
        d = self.repo("ProjA")
        (d / "a.py").write_text("changed\n", encoding="utf-8")
        (d / "b.py").write_text("new\n", encoding="utf-8")
        self.assertEqual(repo_sync_state(d)["pending"], 2)

    def test_unknown_ahead_without_upstream(self):
        """有远端但没跟踪分支时，待推送是「未知」，不能报 0。"""
        b = self.bare("A")
        d = self.repo("ProjA", remote=b)
        st = repo_sync_state(d)
        self.assertEqual(st["remote"], "origin")
        self.assertEqual(st["upstream"], "")
        self.assertEqual(st["ahead"], -1)

    def test_json_is_machine_readable(self):
        b = self.bare("A")
        self.repo("ProjA", remote=b)
        self.repo("ProjB")
        rc, out = self.sync(as_json=True)
        data = json.loads(out)          # 整段输出就是 JSON，没有人类可读前缀
        self.assertEqual(rc, 0)
        self.assertEqual(data["repos"], 2)
        self.assertEqual(data["with_remote"], 1)
        self.assertEqual({r["dir"] for r in data["rows"]}, {"ProjA", "ProjB"})


class Push(SyncBase):
    def test_push_commits_with_manual_subject_and_trailer(self):
        """手动同步没有新对话可依据，提交信息就老实写「手动同步」，并带 trailer。"""
        b = self.bare("A")
        d = self.repo("ProjA", remote=b)
        (d / "a.py").write_text("changed\n", encoding="utf-8")

        rc, out = self.sync(push=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("推送成功", out)
        body = git_out(b, "log", "-1", "--format=%B")
        self.assertTrue(body.startswith("sync: 手动同步（1 项变化）"), body)
        self.assertIn(ARCHIVER_TRAILER, body)

    def test_push_sets_upstream(self):
        b = self.bare("A")
        d = self.repo("ProjA", remote=b)
        self.sync(push=True)
        self.assertEqual(git_out(d, "rev-parse", "--abbrev-ref", "@{u}"), "origin/main")

    def test_second_push_is_noop(self):
        """提交过、推过之后再来一次：不产生新提交，也不算失败。"""
        b = self.bare("A")
        d = self.repo("ProjA", remote=b)
        (d / "a.py").write_text("changed\n", encoding="utf-8")
        self.sync(push=True)
        n = git_out(d, "rev-list", "--count", "HEAD")

        rc, out = self.sync(push=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("无改动", out)
        self.assertEqual(git_out(d, "rev-list", "--count", "HEAD"), n)
        self.assertEqual(len(self.remote_commits(b)), 2)

    def test_repo_without_remote_is_skipped(self):
        d = self.repo("ProjB")
        (d / "a.py").write_text("changed\n", encoding="utf-8")
        rc, out = self.sync(push=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("未配远端", out)
        self.assertEqual(git_out(d, "rev-list", "--count", "HEAD"), "1")

    def test_diverged_push_is_refused_and_remote_untouched(self):
        """远端有本地没有的提交：拒绝推送，远端 HEAD 一个字都不能变。"""
        b = self.bare("C")
        d = self.repo("ProjC", remote=b)
        run_git(d, "push", "-q", "-u", "origin", "main")

        # 另一个人往远端塞了一个提交
        other = self.root / "other"
        other.mkdir()
        run_git(self.root, "clone", "-q", str(b), str(other))
        (other / "theirs.py").write_text("theirs\n", encoding="utf-8")
        run_git(other, "add", "-A")
        run_git(other, "commit", "-q", "-m", "别人的提交")
        run_git(other, "push", "-q", "origin", "main")
        head_before = git_out(b, "rev-parse", "main")

        (d / "mine.py").write_text("mine\n", encoding="utf-8")
        rc, out = self.sync(push=True)

        self.assertEqual(rc, 1, out)
        self.assertIn("不会 force", out)
        self.assertEqual(git_out(b, "rev-parse", "main"), head_before)
        self.assertIn("别人的提交", self.remote_commits(b))

    def test_empty_repo_is_reported_not_silently_skipped(self):
        """git init 了但一次没提交、又配了远端：要出现在结果里。"""
        b = self.bare("E")
        self.repo("ProjE", remote=b, commit=False)
        rc, out = self.sync(push=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("空仓库", out)

    def test_detached_head_is_reported(self):
        b = self.bare("D")
        d = self.repo("ProjD", remote=b)
        run_git(d, "checkout", "-q", "--detach")
        rc, out = self.sync(push=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("分离头指针", out)


class Filtering(SyncBase):
    def test_only_filters_repos(self):
        b = self.bare("A")
        self.repo("ProjA", remote=b)
        self.repo("ProjB")
        rc, out = self.sync(only="proja")
        self.assertEqual(rc, 0)
        self.assertIn("ProjA", out)
        self.assertNotIn("ProjB", out)

    def test_empty_target_exits_zero(self):
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        self.assertIn("没有归档仓库", out)

    def test_only_without_match_exits_one(self):
        """指定了 --only 却一个都没匹配：这是用户的筛选条件没命中，不是空目录。"""
        self.repo("ProjA")
        rc, out = self.sync(only="zzz")
        self.assertEqual(rc, 1)
        self.assertIn("匹配「zzz」", out)


class Helpers(unittest.TestCase):
    def test_short_url_variants(self):
        self.assertEqual(_short_url("git@github.com:someone/x.git"), "github.com/someone/x.git")
        self.assertEqual(_short_url("https://github.com/someone/x.git"), "github.com/someone/x.git")
        self.assertEqual(_short_url("https://user:ghp_secret@github.com/someone/x.git"),
                         "github.com/someone/x.git")
        self.assertEqual(_short_url("ssh://git@example.com:2222/a/b.git"),
                         "example.com:2222/a/b.git")

    def test_short_url_hides_local_paths(self):
        """本地仓库路径整条对读者没意义，只留仓库名。"""
        self.assertEqual(_short_url("/srv/git/x.git"), "本地 x.git")
        self.assertEqual(_short_url("C:/backup/repos/y.git"), "本地 y.git")

    def test_remote_names_prefers_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            run_git(d, "init", "-q", "-b", "main")
            run_git(d, "remote", "add", "zzz", "git@example.com:a/z.git")
            run_git(d, "remote", "add", "origin", "git@example.com:a/o.git")
            names = git_remote_names(d)
            self.assertEqual([n for n, _ in names], ["origin", "zzz"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
