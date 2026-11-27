## 这个 PR 做了什么

<!-- 一句话说清改了什么、为什么改。一个 PR 只做一件事。 -->

## 变更类型

- [ ] `feat` 新功能
- [ ] `fix` 修 bug
- [ ] `docs` 文档
- [ ] `test` 测试
- [ ] `chore` 构建 / 元数据 / 发版

## 影响范围

<!-- 改了哪些文件？是否触碰归档主流程（scripts/archiver.py）？是否改了命令语义？ -->

## 提交前自查

- [ ] `python -m unittest discover -s scripts/tests` 全绿（离线可跑）
- [ ] `python scripts/tests/test_public_content.py` 通过（无真实用户名路径、无竞品名）
- [ ] `python scripts/archiver.py scan --dry-run` 主流程冒烟通过，且未写文件
- [ ] 没有贴真实会话原文、真实项目名或带用户名的绝对路径
- [ ] 新增的适配器有真实会话样本做的夹具（不是猜的 schema）
- [ ] 已在 `CHANGELOG.md` 追加一行（纯文档改动可跳过）
- [ ] 若涉及版本号：`pyproject.toml` / `__version__` / `CHANGELOG` 首条目三处一致

## 验证结果

<!-- 贴关键命令与结果摘要即可，不要贴完整日志。 -->

## 相关 Issue

<!-- 例如：Closes #12 -->
