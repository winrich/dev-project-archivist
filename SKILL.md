---
name: dev-project-archivist
description: 当用户要求「整理/归档我在各个对话里开发的软件和小工具」「把分散在对话窗口里的项目收拢到一个目录」「扫描历史对话提取源码与技术实现」「建立项目总索引并定期同步」时使用。基于本机 WorkBuddy 会话数据库与转录文件（不依赖联网），识别各工作区中开发的项目，归档**可直接继续开发的纯源码**（剔除构建产物、依赖、截图、临时脚本）、项目背景与分模块功能说明，并给出可一键跳回原对话的深链，输出到指定目录并支持增量同步与夜间大模型归纳。
agent_created: true
---

# 对话项目归档器

把散落在上百个对话窗口里的软件项目，收拢成一份**可浏览、可移交、可长期同步**的本地源码档案。

## 何时使用

- 「帮我整理一下这几年在 WorkBuddy 里做过的项目」
- 「对话里开发的小工具都散了，收拢到一个文件夹」
- 「每天自动扫描新做的项目并同步变化」
- 「归档目录要有个总索引，能看到每个项目是干什么的、用什么语言写的」

## 设计目标（四条不可动摇的约束）

1. **归档的是完整源代码，不是文件夹快照**。目标是「他人 clone 下来就能继续开发」：只保留源码、工程文件、配置与必要小资源；构建产物、依赖包、日志、报告输出、截图、临时脚本一律剔除，剔除项登记在 `manifest.json` 可追溯。
2. **每个目录都要小**。目录体积约等于纯代码量，单项目通常几十 KB 到几 MB。大资源（模型、数据集、大图）只登记路径不复制。
3. **确定性解析承担 99% 的工作**。本机转录约 735 MB、写盘类工具调用上万次，任何"把历史对话塞进上下文"的做法都不可行。`archiver.py` 流式解析抽取结构化证据，大模型只处理**压缩后的证据包**（≤14 KB/项目）。
4. **增量 + 幂等 + 可回退**。以项目指纹判定变化，无变化不重写、不调模型；README 划出 `AUTO` 区与 `USER-NOTE` 区，手写内容永不被覆盖；不再归档的目录移入 `_trash/` 而非删除。

## 安装与归档目录

本技能包为**纯 Python 3 标准库**实现，无第三方依赖；`git` 为可选依赖（用于给每个项目初始化独立仓库，缺失时仅跳过该步骤）。

```bash
git clone https://github.com/winrich/dev-project-archivist.git ~/.workbuddy/skills/dev-project-archivist
```

归档输出目录按以下优先级确定，换机或他人使用时无需改代码：

| 优先级 | 方式 | 说明 |
|---|---|---|
| 1 | `--target <路径>` | 命令行显式指定，最高优先级 |
| 2 | 环境变量 `ARCHIVER_TARGET` | 本机固定目录时推荐，免去每次传参 |
| 3 | 默认 | `~/Workbuddy项目归档`（用户主目录下） |

设环境变量（Windows，一次生效）：

```powershell
[Environment]::SetEnvironmentVariable("ARCHIVER_TARGET", "D:\你的归档目录", "User")
```

> 定时任务不保证继承用户环境变量，自动化提示词里应显式写 `--target`。

## 执行流程

### 第 1 步：扫描采集（确定性，约 10–30 秒；首次全量约 5–8 分钟）

```bash
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan
```

产出到目标目录：每个项目的 `source/`（纯源码）、`_docs/`（设计文档）、`manifest.json`、`_evidence/evidence.md`、`_meta/`、`打开原对话.bat`，并为项目目录初始化独立 git 仓库。
同时写 `_evidence/_pending.json`，列出**证据有变化、需要重新归纳**的项目。

首次执行或改过规则后，先跑 `--dry-run` 校准分类：

```bash
python .../archiver.py scan --dry-run          # 只打印分类表，不写任何文件
```

### 第 2 步：大模型归纳（只处理 pending 列表）

```bash
python .../archiver.py pending                 # 打印待归纳项目的 evidence.md 绝对路径
```

对 `pending` 中每个项目：读取 `_evidence/evidence.md`（原始需求、逐轮追加需求、关键决策片段、工具调用画像、项目记忆、文件清单、技术栈信号），归纳后写入同目录 `_summary.json`。

字段固定（不得增删）：

```json
{
  "title": "xx工具 / xx软件 / xx系统",
  "background": "1-2 句：为什么做、要解决什么痛点",
  "purpose": "1-2 句：做什么、谁在用",
  "features": [{"module": "模块名", "items": ["功能点", "功能点"]}],
  "tech_impl": "150-300 字：架构、关键模块、算法或库、数据流",
  "tech_stack": ["技术栈 3-8 条"],
  "language": "主要语言，如 C# / XAML",
  "highlights": ["关键实现要点 3-6 条"],
  "pitfalls": ["踩坑与注意事项，无则空数组"],
  "status": "可用 / 原型 / 半成品 / 已废弃",
  "confidence": 0.8,
  "source": "llm"
}
```

**命名规范（`title`）**：≤14 汉字，必须「主题 + 类别名词」，类别落在 *工具 / 软件 / 系统 / 平台 / 插件 / 类库 / 上位机 / 脚本 / 生成器 / 助手*；不得以「优化 / 分析 / 逆向 / 扩展 / 方案 / 类 / 环境」结尾；不带日期与引号。

**背景与功能**：`background` 写动机不写技术栈；`features` 必须分组（2–5 个模块，每模块 2–5 条），条目用「动词 + 对象」短语，按实际操作顺序排列。

**归纳纪律**：只依据证据包与源码文件清单，禁止编造不存在的功能与库；证据不足就降低 `confidence` 并注明「记录较少」；若只是咨询/方案讨论、未产出可运行软件，`status` 填「原型」或「已废弃」并说明。

### 第 3 步：渲染（确定性）

```bash
python .../archiver.py render
```

合并摘要 → 生成每个项目的 `README.md`、顶层 `INDEX.md` / `index.json`，并按摘要标题**重命名项目目录**（标题变化时才动）。
一条命令走完前两步：`archiver.py run`（scan + render，不含归纳）。

## 回原对话继续开发

已确认 WorkBuddy 官方深链格式为 **`workbuddy://chat/<sessionId>`**，其中 `sessionId` 即 `sessions.id`（与 jsonl 文件名一致）。归档器三处提供入口：

| 入口 | 位置 | 说明 |
|---|---|---|
| README「原始对话」表 | 每个项目 README | 每个会话一行，点「打开对话」直接跳转 |
| `打开原对话.bat` | 项目根目录 | 双击直达最近一次会话 |
| `_meta/conversations.md` | 项目元数据目录 | 该项目**全部用户指令原文**按时间排列，不跳转也能看需求演进 |

手写跳转：浏览器/资源管理器地址栏粘贴 `workbuddy://chat/<会话ID>`。

## 常用命令

| 命令 | 作用 |
|---|---|
| `scan` | 采集/提取/复制源码，写证据包与 `_pending.json` |
| `scan --dry-run` | 只打印分类表，零写入（首次校准必用） |
| `scan --force` | 忽略指纹强制重扫（换机、修规则后用） |
| `scan --only 关键字` | 只处理名称/路径/会话标题命中关键字的项目 |
| `scan --limit N` | 只处理最近 N 个项目 |
| `render` | 由摘要生成 README 与总索引（幂等，可反复跑） |
| `render --no-rename` | 不按摘要标题重命名目录 |
| `run` | scan + render |
| `pending` | 列出待归纳的证据包路径 |
| `status` | 查看上次运行、失联项目、缺摘要项目 |
| `git-clean` | 重建由本工具创建的 git 仓库，清掉历史提交里的大对象 |

## 输出结构

```
<目标目录>/
├── INDEX.md          # 总索引：项目/首次开发/最后开发/最后更新/类型/语言/会话/文件/用途
├── index.json        # 机器可读清单
├── CHANGELOG.md      # 每次运行的变更记录（新增/更新/失联/软删除）
├── overrides.json    # 人工校正表：改标题/背景/用途/类型，keep_scripts 放行脚本，skip 排除工作区
├── state.json        # 指纹库，增量同步的依据
├── _trash/           # 不再归档的项目目录（软删除，可人工恢复）
├── _evidence/_pending.json
└── 2026-03-15_批量重命名工具/
    ├── README.md          # 项目背景/功能说明/技术实现/源码/原始对话/概览
    ├── manifest.json      # 元数据 + 剔除清单 + 外部依赖清单
    ├── .gitignore         # 已排除构建产物，仓库可直接推送远端
    ├── .git/              # 独立仓库，首次归档即完成 init + commit
    ├── 打开原对话.bat      # 双击跳回最近一次会话
    ├── source/            # 纯源码，保持原相对路径
    ├── _docs/             # 设计文档（docx/pdf 等），不进 source
    ├── _evidence/evidence.md
    ├── _meta/
    │   ├── conversations.md  # 全部用户指令原文（时序）
    │   ├── integrity.md      # 工程完整性核对（缺哪些被引用文件）
    │   ├── sessions.json
    │   └── memory/           # 该工作区项目记忆
    └── _summary.json      # 大模型归纳结果（无则模板兜底）
```

## 文件分级规则（决定目录大小的核心）

| 分级 | 判定 | 去向 |
|---|---|---|
| source | 源码扩展名、工程入口文件（`.sln/.csproj/package.json/CMakeLists.txt`…）、配置 | `source/` |
| docs | `.docx/.doc/.pdf/.pptx/.xlsx` | `_docs/` |
| resource | 资源目录（`assets/res/icons/static`）内的小图、字体、小数据文件（≤256 KB） | `source/` |
| bigresource | 同上但超阈值；模型、数据集、大图 | 仅登记 `manifest.json` |
| noise | 构建产物、日志、压缩包、二进制、截图、样本照片、生成的报告 | 剔除，计数 |
| script | 一次性诊断/修复脚本、备份副本 | 剔除，逐个登记路径 |

**脚本剔除的边界**：`build/run/start/deploy/publish/install` 等构建入口白名单保留；`tests/`、`examples/`、`tools/` 下的脚本视为工程组成部分；含 `_backup/_bak/备份/改动前/_copy/副本` 的一律剔除；核心算法脚本（如 `fix_photo_v2.py`）不因 `fix_` 前缀被误剔。误剔可在 `overrides.json` 的 `keep_scripts` 中放行，重跑 `scan --force` 生效。

## 数据源

- 会话元数据：`~/.workbuddy/workbuddy.db` 的 `sessions` 表（只读连接，失败降级为文件扫描）
- 对话转录：`~/.workbuddy/projects/<cwd编码>/<sessionId>.jsonl`，会话 ID 与文件名一一对应
- 工具调用：转录中 `type=function_call` 记录，含 `name` + `arguments`（写盘类用于恢复源码）
- 产物索引：`~/.workbuddy/artifact-index/<sid>.json`
- 文件快照：`~/.workbuddy/file-history/<sid>/<hash>@vN`
- 工作区实体：`~/WorkBuddy/<workspace>/`（AI 从零创建）与其它路径（既有仓库，标记为 `external_repo`）

分类：源码文件数 ≥ 5 且不低于文档数的 30% → 代码项目；否则有文档 → 文档任务。只有代码项目/混合产物建目录，文档任务仅在 `state.json` 留一行；曾归档过但已降级的目录移入 `_trash/`。

## 夜间自动化（降本要点）

定时任务的提示词只需三步串起来（详见 `references/automation-prompt.md`）：
`scan` → 读 `pending` 逐个归纳 → `render`。
因为只有**指纹变化**的项目才进 `pending`，稳定的日子里大模型调用量为 0，成本趋近于零；建议把定时任务绑到低价模型并排在凌晨。

## 已知边界

1. **同一 workspace 被复用于多个主题**时，证据包里的文件清单会偏向"目录里最主要的软件"，摘要可能描述该软件而非某次对话的具体诉求。用 `overrides.json` 覆盖标题/背景/用途，或对该工作区 `skip`。
2. **单项目源码上限**默认 40 MB、单文件 5 MB、资源阈值 256 KB，超出部分只登记在 `manifest.json` 的 `excluded.external`。
3. **跳过目录**含 `bin/obj/node_modules/.git/packages/_tmp*/_probe*/_optdb*` 等；若源码恰好放在这些目录下会被漏掉，需调整 `IGNORE_DIRS` / `IGNORE_DIR_PREFIX`。
4. **图片不进源码**：只有资源目录内的小图才算项目资源，测试样本与截图一律剔除。
5. **正在进行的会话**尾部可能是半行 JSON，解析时逐行跳过，不影响其余内容。
6. **git 仓库体积**：重建归档后若历史里存过旧的大文件，用 `git-clean` 重建仓库。

## 排错

- 报告"目标目录不存在"：确认 `--target` 路径存在或让其自动创建。
- 报告某项目一直"无变化"但确实改过：`scan --force --only 关键字`。
- 摘要明显不对：删掉该项目 `_summary.json` 后重跑 `render` 走模板兜底，或用 `overrides.json` 覆盖。
- 索引里出现 `⚠️失联`：原工作区已被删除，档案与源码副本仍保留。
- 核心脚本被误剔：在 `overrides.json` 加 `{"keep_scripts": ["文件名.py"]}` 后重跑 `scan --force`。
