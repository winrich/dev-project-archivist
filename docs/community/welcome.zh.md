# 欢迎来到对话项目归档器的讨论区

这里是交流使用心得、提问和展示成果的地方。发之前请先看一下下面几条，能省掉来回澄清的时间。

## 这个工具解决什么问题

AI 对话里做出来的软件，三个月后常常连源码在哪都找不到。这个工具扫本机的会话数据，识别你实际开发过的项目，把**纯源码**（去掉依赖、构建产物、截图）收拢成可以独立 `git clone` 继续开发的档案，并附一键跳回原对话的入口。

## 上手只需三步

```bash
git clone https://github.com/winrich/dev-project-archivist.git ~/.workbuddy/skills/dev-project-archivist
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan --dry-run
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py find 你记得的关键词
```

先看分类表对不对，再决定是否真正落盘。全程不联网、零第三方依赖。

## 发帖前请注意

- **不要贴真实会话原文**，里面可能有你的项目路径和指令内容。贴问题时只贴 `scan --dry-run` 的分类表。
- **不要贴带用户名的绝对路径**。用虚构名或占位符代替。
- **安全漏洞不要发在这里**，走 [SECURITY.md](../../SECURITY.zh.md) 里的私有报告入口。

## 欢迎这几类帖子

| 分类 | 适合发什么 |
|---|---|
| **分享** | 你用它找回了什么项目、归档了多少个、有什么意外收获 |
| **问答** | 安装、命令语义、某类工程被误剔 / 漏抽 |
| **适配器** | 想支持的新平台，以及该平台会话数据的真实结构（脱敏后） |
| **建议** | 想要的功能、觉得别扭的地方、文档哪里看不懂 |

## 从哪里开始

- [README](../../README.zh.md)：安装、命令、常见问题
- [DEVELOPMENT.md](../../DEVELOPMENT.zh.md)：架构与命令实现语义
- [CONTRIBUTING.md](../../CONTRIBUTING.zh.md)：怎么提 PR、公开内容三条红线

维护者会尽量回复，但新问题请优先开 Issue（有模板），方便跟踪和回退。
