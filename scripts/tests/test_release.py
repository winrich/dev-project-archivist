#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版口径的单元测试。

守的是一件事：**版本号三处必须同步**。
    pyproject.toml 的 version
    archiver.py 的 __version__
    CHANGELOG.md 的最新**版本**条目

飘了比没有更糟——用户报「0.3.0 有问题」，代码里却是别的数，排查从第一句就错。
这里不查"号该不该是 0.3.0"（那是人的判断），只查三处是不是同一个数。

`## 未发布 · 0.4.0 进行中` 这类标题故意不计入：它不是一个已发布的版本，
把它算进来等于允许"work in progress"顶着版本号。真正发布时再写 `## 0.4.0 · 日期`。

运行：
    python scripts/tests/test_release.py
    或 python -m unittest discover -s scripts/tests
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import archiver  # noqa: E402

# 不依赖 tomllib（3.11+）：项目声明 3.8+，用最小的正则取 version 一行即可
PYPROJECT_VER = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
CHANGELOG_VER = re.compile(r"^##\s+(\d+\.\d+\.\d+)", re.M)


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_matches_code(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = PYPROJECT_VER.search(text)
        self.assertIsNotNone(m, "pyproject.toml 里找不到 version")
        self.assertEqual(m.group(1), archiver.__version__,
                         "pyproject.toml 与 archiver.__version__ 不一致")

    def test_changelog_top_entry_matches(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = CHANGELOG_VER.search(text)
        self.assertIsNotNone(m, "CHANGELOG.md 里找不到版本条目")
        self.assertEqual(m.group(1), archiver.__version__,
                         "CHANGELOG 最新条目不是当前版本（或忘了写）")

    def test_version_flag(self):
        """`--version` 是一句命令就能答的问题，不该让用户去翻文件。"""
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "archiver.py"),
                            "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn(archiver.__version__, (r.stdout or "") + (r.stderr or ""))

    def test_semver_shape(self):
        self.assertRegex(archiver.__version__, r"^\d+\.\d+\.\d+$")


class TestReadmeSamples(unittest.TestCase):
    """README 里贴的输出示例，必须还能在代码里找到出处。

    阶段编号曾经写成 `1/5 → 2/5 → 5/5`，中间两段并不存在。这种编号一旦贴进 README，
    读者只会当成「输出被截断了」，而不会想到是代码写错。这里把示例里每个
    `[i/n] 阶段名` 拿去 archiver.py 里对一遍：对不上，就说明示例比代码旧（或改名忘了改文档）。

    只查这一条，不查具体数字：数字是虚构的，**结构**必须是真的。
    """

    STAGE = re.compile(r"\[(\d+)/(\d+)\] (会话索引|归并完成|扫描完成)")

    def _code_stages(self):
        code = (ROOT / "scripts" / "archiver.py").read_text(encoding="utf-8")
        return set(self.STAGE.findall(code))

    def test_stage_numbering_matches_code(self):
        real = self._code_stages()
        self.assertTrue(real, "archiver.py 里找不到阶段编号（改了日志格式？）")
        for name in ("README.md", "README.zh.md"):
            doc = (ROOT / name).read_text(encoding="utf-8")
            shown = set(self.STAGE.findall(doc))
            self.assertTrue(shown, f"{name} 里找不到阶段示例")
            stale = shown - real
            self.assertEqual(stale, set(),
                             f"{name} 的阶段编号在代码里不存在：{sorted(stale)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
