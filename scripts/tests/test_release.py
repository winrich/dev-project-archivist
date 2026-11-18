#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版口径的单元测试。

守的是一件事：**版本号三处必须同步**。
    pyproject.toml 的 version
    archiver.py 的 __version__
    CHANGELOG.md 的最新条目标题

飘了比没有更糟——用户报「0.3.0 有问题」，代码里却是别的数，排查从第一句就错。
这里不查"号该不该是 0.3.0"（那是人的判断），只查三处是不是同一个数。

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
