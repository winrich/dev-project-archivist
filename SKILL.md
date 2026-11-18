---
name: dev-project-archivist
description: 当用户要求「整理/归档我在各个对话里开发的软件和小工具」「把分散在对话窗口里的项目收拢到一个目录」「扫描历史对话提取源码与技术实现」「建立项目总索引并定期同步」时使用。读取本机 AI 工具的会话数据（内置 WorkBuddy 与 Claude Code 适配器，其他平台可按同一接口扩展，全程不依赖联网），识别各工作区中开发的项目，归档**可直接继续开发的纯源码**（剔除构建产物、依赖、截图、临时脚本）、项目背景与分模块功能说明，并给出可一键跳回原对话的深链，输出到指定目录并支持增量同步与夜间大模型归纳。
agent_created: true
---

# 对话项目归档器

把散落在上百个对话窗口里的软件项目，收拢成一份**可浏览、可移交、可长期同步**的本地源码档案。

## 何时使用

- 「帮我整理一下这几年在 AI 对话里做过的项目」
- 「对话里开发的小工具都散了，收拢到一个文件夹」
- 「我用的是 Claude Code，也想把项目归档起来」
- 「每天自动扫描新做的项目并同步变化」
- 「归档目录要有个总索引，能看到每个项目是干什么的、用什么语言写的」
- 「我上个月写过一个把批处理转 PowerShell 的脚本，在哪来着」→ `find`
- 「这东西我之前是不是做过」→ `find 关键词`，比翻 `INDEX.md` 快得多
- 「这周到底干了多少活 / token 花在哪了」→ `stats --days 7`（不估成本，只报用量）
- 「给我一张图 / 发给领导看 / 我要截图」→ `report --days 30`（单文件 HTML，不含路径与指令原文）
- 「上周那个版本哪去了 / 想看看 11 月 14 号是什么样」→ `restore --date 2026-11-14`（先预览，`--write` 才出副本）
- 「归档想备份到自己仓库 / 怕硬盘坏了」→ `sync` 看状态，配好远端后 `sync --push`

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

产出到目标目录：每个项目的 `source/`（纯源码）、`_docs/`（设计文档）、`manifest.json`、`_evidence/evidence.md`、`_meta/`、`open-session.bat`，并为项目目录初始化独立 git 仓库。
同时写 `_evidence/_pending.json`，列出**证据有变化、需要重新归纳**的项目。

**提交信息**从「上次提交以来的对话指令」生成（`sync: <那天最主要的一件事> 等 N 项` + 逐条正文），
所以 `git log` 是可读的时间线而不是一串 `sync: 同步 X 归档内容`。主题按信息量挑而非按时间挑
（最新的那条常是「还是不行」）；先脱敏再截断；提到 `.ssh` / `.env` 的指令整条丢弃；
没有可用指令时退回原文案，不编造。提交体带 `X-Archiver:` 标记，`git-clean` 据此判断能否安全重建。

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

回跳方式按平台分派，没有深链能力的平台降级为文字说明，**不会产出点不开的死链**。

| 平台 | 深链格式 | 手写跳转 |
|---|---|---|
| WorkBuddy | `workbuddy://chat/<会话ID>` | 浏览器/资源管理器地址栏粘贴 |
| Claude Code | 无 | 在项目目录执行 `claude --resume <会话ID>` |

三处入口：

| 入口 | 位置 | 说明 |
|---|---|---|
| README「原始对话」表 | 每个项目 README | 每个会话一行，有深链时可点「打开对话」，否则标注「无深链」 |
| `open-session.bat` | 项目根目录 | 双击直达最近一次会话；macOS/Linux 生成 `.command`/`.sh`；无深链平台改生成 `open-session.md` 写明恢复方式 |
| `_meta/conversations.md` | 项目元数据目录 | 该项目**全部用户指令原文**按时间排列，不跳转也能看需求演进 |

## 常用命令

| 命令 | 作用 |
|---|---|
| `scan` | 采集/提取/复制源码，写证据包与 `_pending.json` |
| `scan --dry-run` | 只打印分类表，零写入（首次校准必用） |
| `scan --dry-run --all` | 连空壳会话一起列出（默认折叠，只报计数） |
| `scan --explain [关键字]` | 打印判定依据：为什么算代码项目、名字取自哪个证据（零写入） |
| `scan --force` | 忽略指纹强制重扫（换机、修规则后用） |
| `scan --only 关键字` | 只处理名称/路径/会话标题命中关键字的项目 |
| `scan --limit N` | 只处理最近 N 个项目 |
| `render` | 由摘要生成 README 与总索引（幂等，可反复跑） |
| `render --no-rename` | 不按摘要标题重命名目录 |
| `run` | scan + render |
| `--source all\|workbuddy\|claude-code` | 选择数据来源，默认 `all` 自动探测；可逗号组合 |
| `pending` | 列出待归纳的证据包路径 |
| `status` | 查看上次运行、失联项目、缺摘要项目、`.git` 体积体检 |
| `git-clean` | 重建由本工具创建的 git 仓库，清掉历史提交里的大对象 |
| `sync` | **看归档仓库同步状态**：哪些配了远端、待提交几项、待推送几个提交（只读） |
| `sync --push` | 提交未提交的改动并推到各仓库自己配的远端（**未配远端的不推，永不 force**；`--only` 过滤，`--json` 机器可读） |
| `daylog --date YYYY-MM-DD` | **零写入**按天汇总：指令→产出轮次、命令用途、token 用量 + 数据完整性体检（`--json` 输出机器可读） |
| `daylog --merge-external` | 把工作区外的写入按仓库提升为独立行（默认只在体检里计数） |
| `log --date YYYY-MM-DD` | 写出清点稿 `<target>/logs/YYYY-MM-DD.md`：确定性、幂等、已脱敏 |
| `find 关键词` | 跨项目检索：名字/文件名/指令原文，多词全部命中（**只读，不加锁**） |
| `find 关键词 --open` | 打开第一条命中的原对话 |
| `find 关键词 --json` | 输出 JSON 供脚本消费（含 score / snippets） |
| `stats --days 7` | **跨天账本**：最近 N 天的对话/项目/文件/token + 每日柱状（只读，`--date` 指定截止日，`--json` 机器可读） |
| `report --days 30` | **写成单文件 HTML 账本**：热力图 + 柱状图 + 项目卡片（不含路径与指令原文，可直接分享；`--out` 指目录，`--open` 打开） |
| `restore` | 列出能回到哪些天（各归档仓库有提交的日子，新→旧） |
| `restore --date YYYY-MM-DD` | **预览**该日结束时的快照：每个项目取到哪个提交/多少文件/多大，零写入 |
| `restore --date YYYY-MM-DD --write` | 展开成副本到 `<target>/_restore/<date>/`，**不覆盖归档**（`--only` 过滤，`--out` 换位置，`--force` 覆盖已有副本） |
| `restore --json` | 输出 JSON（含 `commit` / `snapshot_at` / `files` / `bytes`） |

## 按天汇总（`daylog`，只读）

需要「某天做了什么」而不是「某个项目是什么」时用它。与归档共用转录，但按**事件时间**切天，
因此跨天会话会被拆成多条，而不是只挂在首末时间上。

```bash
python .../archiver.py daylog --date 2026-11-18
```

**四条体检指标必须一起读**，不能只看表格：

| 指标 | 含义 | 异常时的动作 |
|---|---|---|
| 无时间戳事件 | 事件前没有 ts，归不到任何一天 | 查适配器是否漏发 `ts` 事件 |
| 工作区外写入 | 写盘落在 cwd 之外，未计入表格 | 占比高说明工作区划分与实际干活目录不一致 |
| 重复指令压重 | 上下文压缩导致的原文重放已去掉 | 为 0 属正常，偏大说明会话历经多次压缩 |
| 跨天会话 | 同一会话出现在多天 | 正常现象，说明按天切分生效 |
| 无 token 记录的会话 | 该平台不写 usage | **留空，不可填 0**，否则「便宜」与「没数据」无法区分 |

**token 口径**：`cache_read` 是 `input` 的子集（`total = input + output` 成立）。
成本换算用 `全价部分 = input − cache_read`，两者不能相加。缓存命中率接近 99% 属长会话常态。

`daylog` 目前不写任何文件、不调模型。指标异常时先修适配器，不要直接拿这张表去生成正式日志。

## 落盘（`log`）

```bash
python .../archiver.py log --date 2026-11-18              # → <target>/logs/2026-11-18.md
python .../archiver.py log --date 2026-11-18 --out ./logs
```

清点稿只写事实（项目、时段、指令→产出、token、区外改动、数据缺口），**不写「完成/进行中/阻塞」**：
那需要读懂上下文，属模型职责；用启发式硬猜会产出具结论外形而无依据的内容。

三条硬约束，改动时不能破：

| 约束 | 表现 | 破坏后果 |
|---|---|---|
| 幂等 | 同样输入 → 同样字节，二次运行只打印「无变化」 | 定时任务每天产生无意义 diff，用户直接卸载 |
| 脱敏 | 提示词过 `desensitize`；`.ssh`/`.aws`/`.env`/密钥路径只计数不列名 | 凭据位置被贴进群/日志系统 |
| 隔离 | 只写 `logs/`，不碰归档目录 | 无法回退，档案被日志污染 |

## 按日恢复（`restore`）

```bash
python .../archiver.py restore                            # 能回到哪些天
python .../archiver.py restore --date 2026-11-14          # 预览，零写入
python .../archiver.py restore --date 2026-11-14 --write  # → <target>/_restore/2026-11-14/
```

语义（回答用户时必须说清，含糊一句就会让人误以为这是备份/回滚）：

| 是 | 不是 |
|---|---|
| 只从**归档仓库自己的 git 历史**取（`git archive`，不碰工作区与索引） | 不从原工作区取——原工作区可能已删除或改写 |
| 「**该日结束时**归档里长什么样」 | **不是「那天改了什么」**（那是 `log` / `stats`） |
| 只含**当时已归档**的文件 | 不是备份：媒体、超大二进制、敏感路径、`.gitignore` 排除项、首次归档之前的文件都不在快照里 |

两条边界必须显式报告，不能静默少一行：某天没跑归档 → 退到更早的最近一次提交并打出它的真实时间；
项目晚于目标日才首次归档 → 归入「无法恢复」并写明首次归档日期。

**永不覆盖归档**：写入点固定为当天新建的独立目录；同日再写默认拒绝（`--force` 才覆盖）；
总量超 800MB 需 `--force` 确认。副本不带 `.git`，并附 `_restore.md` / `_restore.json` 记录每个项目的来源 commit。

## 备份到用户的仓库（`sync`）

```bash
python .../archiver.py sync            # 看：哪些配了远端、待提交、待推送（零写入）
python .../archiver.py sync --push     # 做：提交 + 推送
```

`run` 的提交只留在本机。`sync` 把本地提交推到**用户自己配的**远端，这是归档能扛住磁盘故障的唯一一环。

三条约束（回答用户时不要含糊）：

| 约束 | 说明 |
|---|---|
| 只推**配了远端**的仓库 | 不替用户决定备份到哪儿、不猜地址、不自动建远端；没配就跳过并给出 `git remote add` 写法 |
| **永不 force** | 远端有本地没有的提交时只报告。归档里是源码，强推一次可能抹掉别人的改动 |
| 不编造提交信息 | 手动同步没有新对话可依据，写 `sync: 手动同步（N 项变化）` |

**不在 `run` 里自动推**：定时任务不该依赖网络，断网或凭据过期会把一次成功的归档变成一次报错的定时任务。

## 输出结构

```
<目标目录>/
├── INDEX.md          # 总索引：项目/首次开发/最后开发/最后更新/类型/语言/会话/文件/用途
├── index.json        # 机器可读清单
├── CHANGELOG.md      # 每次运行的变更记录（新增/更新/失联/软删除）
├── overrides.json    # 人工校正表：改标题/背景/用途/类型，keep_scripts 放行脚本，skip 排除工作区
├── state.json        # 指纹库，增量同步的依据
├── _trash/           # 不再归档的项目目录（软删除，可人工恢复）
├── _restore/         # restore 产出的历史快照副本（归档本身不动）
├── _evidence/_pending.json
└── 2026-03-15_批量重命名工具/
    ├── README.md          # 项目背景/功能说明/技术实现/源码/原始对话/概览
    ├── manifest.json      # 元数据 + 剔除清单 + 外部依赖清单
    ├── .gitignore         # 已排除构建产物，仓库可直接推送远端
    ├── .git/              # 独立仓库，首次归档即完成 init + commit
    ├── open-session.bat  # 双击跳回最近一次会话
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

平台差异全部封装在 `scripts/adapters/` 里，归档主流程只消费统一的中立事件。

| 平台 | 适配器 | 会话数据位置 | 深链 |
|---|---|---|---|
| WorkBuddy | `workbuddy.py` | `~/.workbuddy/workbuddy.db` + `projects/<cwd编码>/<sid>.jsonl` | `workbuddy://chat/<会话ID>` |
| Claude Code | `claude_code.py` | `~/.claude/projects/<路径编码>/<会话ID>.jsonl` | 无，降级为 `claude --resume <会话ID>` |

- `--source` 选择来源：默认 `all`（自动探测本机装了哪些），也可写 `workbuddy`、`claude-code` 或逗号组合。某平台在本机没有数据时自动跳过，不报错。
- 工作区实体即会话的 `cwd`：落在 `~/WorkBuddy/` 下标记为「AI 从零创建」，其余标记为 `external_repo`。
- 项目记忆：WorkBuddy 读 `<cwd>/.workbuddy/memory/*.md`；Claude Code 无此概念，自动跳过，不影响其余归档。

### 新增一个平台

在 `scripts/adapters/` 下新建文件，实现 `Adapter` 子类，再到 `__init__.py` 末尾 `register()`，主程序一行不用改：

```python
class MyAdapter(Adapter):
    name = "my-tool"           # 也是 --source 的取值
    display = "My Tool"
    def probe(self) -> bool: ...               # 本机是否存在该平台数据
    def load_index(self): ...                  # 返回 Index：ids() / info(sid) / transcript(sid)
    def iter_events(self, path, cwd=""): ...   # 逐条产出 7 种中立事件
```

事件规范与辅助函数（`rel_in_cwd` / `clean_prompt` / `iso_to_ms`）见 `scripts/adapters/base.py` 顶部注释。两条硬要求：`info()` 里的 `created` / `updated` 必须是**毫秒整数**（否则跨平台排序会抛 TypeError）；工具调用路径必须经 `rel_in_cwd()` 归一，工作区外的写入一律丢弃。改完运行 `python scripts/tests/test_adapters.py`。

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
