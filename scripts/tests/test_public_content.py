#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开内容的防泄漏测试。

这个仓库里的每个字节都会被推到 GitHub。三类东西不该出现在里面：

    1. **竞品名称**——讲功能不需要点别人的名
    2. **本机真实项目名**——README 的示例输出来自真实归档，直接抄就把用户目录名公开了
    3. **带用户名的绝对路径**——`C:/Users/<某人>/...`

第 2 类不能靠"禁止名单"检查：名单本身就写着那些名字，等于把要藏的东西刻在门上。
所以这里只做**结构性检查**（有没有 `<user>` 形态的路径、有没有竞品字样），
真实项目名靠示例统一用虚构名来保证——`README` 里的 `BatchRenamer` / `InvoiceParser`
就是这套虚构名，测试数据也共用同一批。

运行：
    python scripts/tests/test_public_content.py
    或 python -m unittest discover -s scripts/tests
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_EXT = {".md", ".py", ".toml", ".yml", ".yaml", ".txt", ".json", ".cmd", ".bat", ".sh"}
SKIP_DIRS = {"__pycache__", ".git", "_restore", "_logs_smoke", ".pytest_cache"}
# 本文件自己既写规则又写示例（`C:/Users/<占位>` 之类），扫自己只会自己撞自己
SKIP_FILES = {Path(__file__).name}

# 竞品名：只查这一个，别的一律不点名。
# 拆成两段拼接，是为了让这个文件自己也不含那个字样——否则 `grep` 全仓库时
# 唯一命中的是"守护者"本身，看着像没清干净。
COMPETITOR = re.compile("spec" + "story", re.I)

# `C:/Users/xxx` / `C:\Users\xxx`：只允许明显虚构的占位名
HOME_PATH = re.compile(r"[Cc]:[\\/]Users[\\/]([^\\/\s`'\"]+)")
ALLOWED_HOME = {"me", "x", "u", "someone", "user", "you", "name", "<user>", "用户名"}


def text_files():
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        if p.name in SKIP_FILES or any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


class TestPublicContent(unittest.TestCase):
    def test_no_competitor_name(self):
        bad = []
        for p in text_files():
            try:
                s = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if COMPETITOR.search(s):
                bad.append(str(p.relative_to(ROOT)))
        self.assertEqual(bad, [], f"以下文件出现了竞品名称：{bad}")

    def test_no_real_home_paths(self):
        """`C:/Users/<真名>` 是最容易被顺手抄进 README 的东西。"""
        bad = []
        for p in text_files():
            try:
                s = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in HOME_PATH.finditer(s):
                if m.group(1).lower() not in ALLOWED_HOME:
                    bad.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
        self.assertEqual(bad, [], "以下位置出现了带用户名的绝对路径：" + "; ".join(bad[:5]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
