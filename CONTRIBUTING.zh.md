# 贡献指南

感谢你愿意让这个工具变得更好。贡献不需要联网、不需要账号，也不需要装任何第三方依赖——**Python 标准库 + git 就够**。

[English version](CONTRIBUTING.md)

---

## 一、最有价值的三类贡献

| 类型 | 为什么重要 | 入口 |
|---|---|---|
| **新平台的适配器** | 每支持一个 AI 工具，就解锁一批原本用不上这个工具的人。这是唯一可重复的增长点 | [适配新平台 Issue 模板](.github/ISSUE_TEMPLATE/adapter.md) |
| **文件抽取规则修正** | 某类工程被误剔 / 漏抽，直接影响归档能不能直接 clone 继续开发 | [Bug 报告模板](.github/ISSUE_TEMPLATE/bug.md) |
| **文档可读性** | 中英文措辞、示例、命令语义的说明。好文档比多一个命令更能让人用起来 | 直接开 PR |

适配器接入不用改主流程：按 `scripts/adapters/base.py` 的接口实现 `probe()` / `load_index()` / `iter_events()` / `deep_link()`，再到 `scripts/adapters/__init__.py` 注册两步即可。具体字段含义见 [DEVELOPMENT.zh.md](DEVELOPMENT.zh.md) 的「适配器接口」一节。

> **硬要求**：不要凭猜测定 schema。适配器必须建立在真实会话样本上——先在本机装上该平台、产生真实对话、跑 `probe()` 确认结构，再写代码并给真实样本做的夹具。否则装上去解析出 0 条，比不支持更伤。

## 二、本地开发环境

```bash
git clone https://github.com/winrich/dev-project-archivist.git
cd dev-project-archivist
python --version    # 3.8+
git --version       # 可选，用于给每个归档项目初始化独立仓库
```

零第三方依赖，不需要 `pip install`。测试用标准库 `unittest`，全部离线可跑。

## 三、提交前检查（每项都要过）

```bash
python -m unittest discover -s scripts/tests   # 全量，离线可跑
python scripts/tests/test_public_content.py     # 公开内容防泄漏
python scripts/archiver.py scan --dry-run       # 归档主流程冒烟
```

| 检查项 | 为什么 |
|---|---|
| 全量测试绿 | 250 例覆盖了命名、检索、同步、报告、管理台等，改动容易误伤别处 |
| 公开内容防泄漏 | 这个仓库的每个字节都会推到 GitHub，不能出现真实用户名路径、真实项目名、竞品名 |
| `scan --dry-run` | 确认主流程没有被破坏，且不写任何文件 |

## 四、公开内容三条红线

这个仓库是公开的，任何提交都会被所有人看到。`scripts/tests/test_public_content.py` 会自动拦其中两类，第三类靠自觉：

1. **不点竞品名称**。讲功能差异不需要点别人的名，测试里这条是硬拦的。
2. **不带用户名的绝对路径**。`C:/Users/用户名/...` 这种一律换成白名单占位符或虚构路径，硬拦。
3. **不写真实项目名 / 真实用量**。README 和样例里的 `BatchRenamer`、`InvoiceParser` 都是虚构名，示例数据全编。真实项目名会暴露你的工作目录与业务信息，这条靠自觉——因为禁止名单本身就会把要藏的东西刻在门上。

## 五、PR 规范

- 一个 PR 只做一件事，便于回退；改动前先开 Issue 说明想做什么，尤其是新增适配器。
- 标题用 conventional 前缀：`feat:` / `fix:` / `docs:` / `test:` / `chore:`。
- 按 `.github/PULL_REQUEST_TEMPLATE.md` 填变更说明与自查清单。
- 在 [CHANGELOG.md](CHANGELOG.md) 追加一行（纯文档改动可跳过）。
- 版本号口径：**版本号 = 已完成档位的最高一个**，不提前虚报、不落后漏报。`pyproject.toml` 的 `version`、`scripts/archiver.py` 的 `__version__`、`CHANGELOG.md` 最新条目标题三处必须一致，`test_release.py` 会核对。

## 六、报告问题

开 Issue 前请先跑一次 `python scripts/archiver.py scan --dry-run`，只贴分类表，**不要贴对话原文**——那里面可能有你的项目路径和指令内容。

安全相关问题不要开公开 Issue，见 [SECURITY.zh.md](SECURITY.zh.md)。

## 七、行为准则

尊重每一位贡献者。技术讨论就事论事，不人身攻击、不贬低其他工具的用户。维护者可以关闭偏离主题或不符合上述红线的 Issue / PR，并说明理由。
