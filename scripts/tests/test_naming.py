#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目命名与判定依据的单元测试。

守的是一条链：**名字要来自磁盘上的事实，不能来自用户随口说的那句话。**

实测踩过的坑都固化成了用例：
    · 标题 "https://..." 被冒号切断 → 项目名叫 `https`
    · 目录名 `CDN`（三字母）被长度规则误杀 → 退回「C# 项目」
    · 三个项目都退回语言兜底 → 全叫「Python 项目」，分不清彼此

运行：
    python scripts/tests/test_naming.py
    或 python -m unittest discover -s scripts/tests
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import archiver  # noqa: E402
from archiver import (  # noqa: E402
    _accept_identity, _clean_identity, identity_name, is_junk_title,
    manifest_name, pick_title, short_title, url_host_name,
)


class TestJunkTitle(unittest.TestCase):
    def test_placeholder(self):
        for t in ("E", "summary", "image#3", "screenshot1", "新对话", "12345"):
            self.assertTrue(is_junk_title(t), t)

    def test_url_and_path(self):
        for t in ("https", "http://x.com", "https://www.example.org/chapterq",
                  "C:/Users/me/WorkBuddy", "~/projects", "./demo",
                  "github.com", "git@github.com:x/y"):
            self.assertTrue(is_junk_title(t), t)

    def test_sentence(self):
        # 长句且带疑问/请求语气：是「一句话」，不是「一个名字」
        for t in ("密码为什么为空 LoginPanel", "帮我看看这个报错是什么原因导致的"):
            self.assertTrue(is_junk_title(t), t)

    def test_short_sentence_kept(self):
        # 短句仍保留：信息量还在，换成「C# 项目」反而更难认
        self.assertFalse(is_junk_title("帮我安装适合版本的git"))

    def test_normal_titles_kept(self):
        for t in ("BatchRenamer", "修复导入菜单无法打开窗体",
                  "阅读器 7 第三方系统安装", "对比开源阅读器插件"):
            self.assertFalse(is_junk_title(t), t)


class TestShortTitle(unittest.TestCase):
    def test_url_not_split_at_scheme(self):
        # 旧版按冒号切，https://... 变成 "https"
        self.assertTrue(short_title("https://www.example.org/chapterq?cid=17").startswith("https://"))

    def test_split_at_normal_colon(self):
        self.assertEqual(short_title("项目说明: 这是一个归档器"), "项目说明")

    def test_strip_comment_prefix(self):
        self.assertEqual(short_title("// UTF8ToUnicode.cpp : 定义 DLL"), "UTF8ToUnicode.cpp")

    def test_truncate(self):
        self.assertEqual(len(short_title("一" * 50, 10)), 10)


class TestPickTitle(unittest.TestCase):
    def test_prefer_name_like(self):
        got = pick_title(["帮我写个脚本，要快", "报表导出器"])
        self.assertIn("报表", got)

    def test_url_falls_back_to_host(self):
        got = pick_title(["https://www.example.org/chapterq?cid=17&cno=1"])
        self.assertEqual(got, "example.org")

    def test_sentence_only_keeps_sentence(self):
        # 三个项目都退语言兜底就会全叫「Python 项目」，彼此无法区分
        got = pick_title(["我们的数据分析报告不够深入,难以支持业务决策"])
        self.assertTrue(got.startswith("我们的数据分析报告"))

    def test_empty(self):
        self.assertEqual(pick_title([]), "")
        self.assertEqual(pick_title(["E"]), "")


class TestCleanIdentity(unittest.TestCase):
    def test_https(self):
        self.assertEqual(_clean_identity("https://github.com/winrich/dev-project-archivist.git"),
                         "dev-project-archivist")

    def test_scp_like(self):
        self.assertEqual(_clean_identity("git@github.com:someone/order-sync.git"),
                         "order-sync")

    def test_trailing_slash(self):
        self.assertEqual(_clean_identity("https://gitlab.com/org/repo/"), "repo")


class TestAcceptIdentity(unittest.TestCase):
    def test_short_name_kept(self):
        # 实测回归：CDN 被「长度<4 即垃圾」误杀，项目名退化成「C# 项目」
        for t in ("CDN", "QMS", "OCR"):
            self.assertTrue(_accept_identity(t), t)

    def test_generic_rejected(self):
        for t in ("src", "app", "project", "www", "dist"):
            self.assertFalse(_accept_identity(t), t)

    def test_url_rejected(self):
        self.assertFalse(_accept_identity("https://x.com"))
        self.assertFalse(_accept_identity("https"))
        self.assertTrue(_accept_identity("example.org"))

    def test_too_long(self):
        self.assertFalse(_accept_identity("x" * 41))


class TestManifestName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-name-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_package_json(self):
        self._w("package.json", json.dumps({"name": "@scope/order-sync"}))
        self.assertEqual(manifest_name(self.tmp, ["package.json"]), "@scope/order-sync")

    def test_csproj_assembly_name_wins(self):
        self._w("App/App.csproj", "<Project><PropertyGroup>"
                                   "<AssemblyName>LedgerSync</AssemblyName>"
                                   "</PropertyGroup></Project>")
        self.assertEqual(manifest_name(self.tmp, ["App/App.csproj"]), "LedgerSync")

    def test_csproj_filename_fallback(self):
        self._w("App/Ledger.csproj", "<Project><PropertyGroup></PropertyGroup></Project>")
        self.assertEqual(manifest_name(self.tmp, ["App/Ledger.csproj"]), "Ledger")

    def test_pyproject(self):
        self._w("pyproject.toml", '[project]\nname = "dev-project-archivist"\n')
        self.assertEqual(manifest_name(self.tmp, ["pyproject.toml"]), "dev-project-archivist")

    def test_go_mod(self):
        self._w("go.mod", "module github.com/example/order-sync\n\ngo 1.22\n")
        self.assertEqual(manifest_name(self.tmp, ["go.mod"]), "order-sync")

    def test_ambiguous_csproj_gives_up(self):
        # 同层多个 csproj = 一个仓库里塞了多个工程，无法判断谁是主角
        for n in ("A/A.csproj", "B/B.csproj"):
            self._w(n, "<Project/>")
        self.assertEqual(manifest_name(self.tmp, ["A/A.csproj", "B/B.csproj"]), "")

    def test_no_manifest(self):
        self.assertEqual(manifest_name(self.tmp, ["src/main.py"]), "")


class TestIdentityName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa-id-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_platform_dir_rejected(self):
        # 平台自动生成的时间戳目录不能当名字
        for name in ("2026-11-18-21-34-40", "20260328171213"):
            d = self.tmp / name
            d.mkdir()
            self.assertEqual(identity_name(d, []), ("", ""))

    def test_dirname_used(self):
        d = self.tmp / "CDN"
        d.mkdir()
        self.assertEqual(identity_name(d, []), ("CDN", "工作区目录名"))

    def test_manifest_beats_dirname(self):
        d = self.tmp / "BatchRenamer"
        d.mkdir()
        (d / "pyproject.toml").write_text('[project]\nname = "task-board"\n', encoding="utf-8")
        self.assertEqual(identity_name(d, ["pyproject.toml"]), ("task-board", "工程清单"))

    def test_git_remote_beats_manifest(self):
        if not shutil.which("git"):
            self.skipTest("未安装 git")
        d = self.tmp / "some-dir"
        d.mkdir()
        (d / "pyproject.toml").write_text('[project]\nname = "from-manifest"\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(d)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin",
                        "git@github.com:someone/real-repo.git"], check=True,
                       capture_output=True)
        self.assertEqual(identity_name(d, ["pyproject.toml"]), ("real-repo", "git remote origin"))

    def test_git_remote_strips_token(self):
        if not shutil.which("git"):
            self.skipTest("未安装 git")
        d = self.tmp / "leaky"
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin",
                        "https://user:ghp_secret@github.com/someone/repo-x.git"],
                       check=True, capture_output=True)
        name, src = identity_name(d, [])
        self.assertEqual((name, src), ("repo-x", "git remote origin"))
        self.assertNotIn("ghp_secret", name)


class TestUrlHost(unittest.TestCase):
    def test_host(self):
        self.assertEqual(url_host_name("https://www.example.org/chapterq?cid=17"), "example.org")
        self.assertEqual(url_host_name("www.example.com/a/b"), "example.com")

    def test_not_url(self):
        self.assertEqual(url_host_name("项目说明"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
