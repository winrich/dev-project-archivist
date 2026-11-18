# daylog 实现与数据体检报告

> 工作副本：`C:/Users/me/WorkBuddy/2026-11-18-21-34-40/dpa/`
> 你的 GitHub 仓库（`winrich/dev-project-archivist`）**未做任何改动**，以下改动都在副本里，可直接比对后再提交。

---

## 一、命令现在能跑了

```bash
python scripts/archiver.py daylog --date 2026-11-18 --dry-run
```

`daylog` 是新增子命令，`--dry-run` 被接受但等价于默认行为——**它本来就是只读的**：不写文件、不调模型、不加锁。

输出的四个部分：会话漏斗 → 工作区表格 → 数据完整性体检 → 指令明细。

## 二、数据齐不齐：结论是「齐，但有三处会让人误判」

拿你本机 25 个会话、真实 735MB 转录跑下来，逐日结果：

| 日期 | 会话 | 指令 | 写 | 改 | 工具 | 结论 |
|---|---|---|---|---|---|---|
| 2026-11-18 | 2 | 13 | 5 | 4 | 156 | 数据完整，可直接写日志 |
| 2026-11-16 | 2 | 31 | 7 | 5 | 764 | **表格严重低估工作量**（见发现 3） |
| 2026-11-14 | 2 | 90 | 77 | 10 | 3202 | 跨天会话被正确切分，去重压掉 39 条 |

### 发现 1：`is_playground` 不是「自动化」，差点把日志清空

第一版按 `automation or playground` 过滤，结果 25 个会话里 18 个被当成后台任务扔掉，当天命中 0 条。

查了库：`is_playground = 1` 有 20 条、`is_background_automation` 全是 `NULL`。**`is_playground` 表示「在对话界面创建」，是所有正常会话的默认值。** 现在只按 `automation` 过滤。

> 这正是「先跑一遍再写渲染」的价值：如果直接写日志生成，会得到一份「今天什么都没干」的报告。

### 发现 2：驼峰 `filePath` 导致 127 次写盘被静默丢弃

适配器只认 `file_path`，但同一批数据里两种写法并存：

| 参数键组合 | 次数 |
|---|---|
| `filePath, new_str, old_str` | 58 |
| `explanation, filePath, new_str, old_str` | 41 |
| `content, filePath` | 28 |
| `content`（无路径） | 7 |

**其中 28 次是 `Write` 完整文件写入**——这不只影响日报，**归档的 `source/` 一直缺这些文件**。已在两个适配器里补上 `filePath` / `notebookPath`。

### 发现 3：69% 的写盘落在工作区外（最需要你决策的一条）

2026-11-16 那天：

```
写盘调用          209 次
    工作区内         64
    工作区外        145   ← 主要去向 some-plugin(88)、tools(39)、reader-private(11)
```

会话的 cwd 是 `C:\Users\me\WorkBuddy\2026-11-11-19-50-35`，但实际在 `some-plugin` 等目录干活。归档侧按设计丢弃工作区外写入（这是对的，防止把系统配置写进档案），**但对日志来说，那 145 次才是真正的工作**。

目前处理：数字如实暴露 + 触发警示，不擅自改归属。是否把外部目录提升为「伪工作区」，需要你定（见第五节）。

### 发现 4：上下文压缩摘要被当成用户指令

`Summary of the conversation between an AI agent and a user. All tool calls...` 这类压缩摘要被平台存成 user 消息，会混进指令流。已在 `clean_prompt` 里按前缀拦掉，11-16 的指令从 31 条降到 29 条。

同时压缩会把早期提问**原文重放**，11-14 那天压掉 39 条重复。短指令（「继续」「好」）不压——它们是真的动作。

### 发现 5：2 个会话在数据库里没有行

`310e409c`、`637be26a` 只存在于 `projects/*.jsonl`，DB 里查不到，因而没有 cwd，被整体丢弃。但**转录头部自带 `cwd` 和 `timestamp`**。已加 `_head_info()`，从头部补出元数据。

---

## 三、为此改了什么（按文件）

| 文件 | 改动 | 为什么 | 影响面 |
|---|---|---|---|
| `scripts/adapters/base.py` | 契约文档补 `op` 字段；`clean_prompt` 增加压缩摘要前缀过滤 | 摘要不是提问 | 归档与日志同时变干净；`conversations.md` 会少几条噪声 |
| `scripts/adapters/workbuddy.py` | 新增 `EDIT_TOOLS`；认 `filePath`；`Edit` 发路径事件；工作区外写入发 `external` 事件；新增 `_head_info()` | 见发现 2、3、5 | `written` 变多（补回 28 个文件）；`touched` 新增，归档不用 |
| `scripts/adapters/claude_code.py` | 同上（`EDIT_TOOLS`、`filePath`、`external`） | 保持一致 | 同上 |
| `scripts/archiver.py` | `SessionParser.touched`；新增 `DayCollector` / `run_daylog` / `day_range_ms` / `to_ms`；`open-script` 跨平台化；`main` 加 `daylog`、`--date`、`--json` | daylog 必需 | 归档主流程逻辑未变（只多了一个 `elif rel:` 分支，不写 `written`） |
| `scripts/tests/test_daylog.py` | **新增** 11 个测试 | 守「不丢不重」 | — |
| `.github/workflows/ci.yml` | **新增** CI（Windows + Ubuntu × 3.8/3.11/3.13） | 无 CI 无法自证维护 | — |
| `pyproject.toml` | **新增**，支持 `pipx` / `uvx` 安装，`dependencies = []` | 降低安装摩擦 | — |
| `.github/ISSUE_TEMPLATE/` | **新增** bug / adapter 两个模板 | 接外部 PR | — |
| `README.md` `README.en.md` `SKILL.md` | 新增 daylog 章节与命令；`打开原对话.bat` → `open-session.bat`（macOS `.command` / Linux `.sh`） | 中文名在 zip/git/CI 下会乱码，`.bat` 在非 Windows 是废文件 | 渲染出的文件名变化 |

代码量：`archiver.py` 2400 → 2786 行，其中 daylog 约 330 行；测试 11 → 22 个。

---

## 四、验收情况

| 项 | 结果 |
|---|---|
| `python -m unittest discover -s scripts/tests` | **22 tests OK** |
| 分桶不丢不重（跨 3 天会话） | 各日指令数之和 == 去重后总数 ✔ |
| `render` 连跑两次 | 全部 md / index.json 的 md5 一致，**幂等通过** ✔ |
| `scan --target /tmp/... --limit 3` | 正常产出 `source/`、`open-session.bat`，无异常 ✔ |
| `scan --dry-run`（分类表） | 与改动前一致：代码项目 8 / 文档任务 4 ✔ |
| 空数据环境 | `daylog`、`scan --dry-run` 均正常退出 0 ✔ |

⚠️ 一个已知的行为变化：补回 `filePath` 后，**下次 `scan --force` 会让部分项目多出源码文件**，指纹变化会进 `pending`。这是一次性修正，不是抖动。

---

## 五、还没解决的三件事（需要你定）

| 问题 | 现状 | 建议 |
|---|---|---|
| **外部写入怎么归属** | 只统计不归类，145 次写盘在表格外 | 方案 A：提升为「伪工作区」单独成行（更像真日志，但要处理跨会话合并）；方案 B：保持现状只在明细列出（安全，但正文要人工看）。我倾向 A，加 `--merge-external` 开关 |
| **daylog 落盘** | 只有预览 | 下一步生成 `logs/YYYY-MM-DD.md`，套 `{完成/进行中/阻塞/证据}` 四段，脱敏后写入 |
| **Cursor 适配器** | 只有 WorkBuddy / Claude Code | 你本机没装 Cursor，只能按规范盲写 + 单测，做不到实测 |

---

## 六、怎么合到你的仓库

副本目录结构与你仓库一致，直接覆盖即可：

```bash
# 先看差异（推荐）
diff -ru ~/你的仓库 dev-project-archivist工作副本 -x .git -x __pycache__

# 覆盖并跑测试
cp -r dpa/scripts dpa/.github dpa/pyproject.toml dpa/README.md dpa/README.en.md dpa/SKILL.md <你的仓库>/
cd <你的仓库> && python scripts/tests/test_adapters.py && python scripts/tests/test_daylog.py
```

提交建议拆三个 commit，便于回退：

1. `fix(adapters): 认 filePath、保留 Edit 路径、补 jsonl 元数据`
2. `feat(daylog): 按天聚合与数据完整性体检（只读）`
3. `chore: 加 CI / pyproject / issue 模板，打开脚本跨平台化`

---

## 七、第二轮：对齐同类工具的六项改造

同类工具（开源仓库 + 独立 CLI）的采集层有 17 个 provider 和一套明确的 SPI。
逐项比对后，**它的架构我们已经有（adapter 契约 + 中立事件流）**，真正的差距在「事件的信息密度」：
它的事件带命令、token、仓库身份、超长行保护，我们的事件只有「发生了什么」。

这一轮补的就是这六项，全部落在 `base.py` 的契约上，主程序没变复杂。

| 能力 | 之前 | 现在 | 为什么重要 |
|---|---|---|---|
| **轮次配对** | 按天收集，产出两份平行清单 | `DayCollector` 在采集时就把「指令 → 文件/命令/错误」配成一条 `turn` | 日志的最小单元不是「一天」，是「一句话 → 它带来的产出」 |
| **命令用途归类** | 只有工具计数 | `extract_cmd` 取首行并剥掉 `cd … &&` 前缀，`cmd_class` 归成 脚本/构建/测试/依赖/git/查看 | 「跑了 3 次构建、5 次测试」能写进汇报，「执行了 430 条命令」不能 |
| **token 用量** | 完全没采 | 新增 `EV_USAGE` 事件，`usage_event()` 收敛三家字段名 | 这是成本侧与产出侧唯一的连接键；没有它，日志只能记「做了什么」，答不了「花了多少」 |
| **仓库身份** | 只有工作区路径 | `git_identity()` + `normalize_git_url()`，把 https / ssh / scp 三种写法折成 `host/owner/repo` | 同一仓库换协议、换机器就成「新项目」，是归档类工具最常见的重复项来源 |
| **超长行保护** | `for line in f` | `safe_lines()`，单行超阈值丢弃并继续 | 进程被杀会留下半行；一行吃掉整个内存是真实存在的崩溃 |
| **工具分类** | 只认工具名 | `tool_type()` 收敛成 写盘/读/检索/命令/任务/其它 6 类 | 跨平台可累加，否则 WorkBuddy 的 `run_command` 和 Claude 的 `Bash` 永远对不上 |

### 真实数据暴露的三个新问题

**1. 命令分类原本几乎全错。** 6117 条命令里 5608 条落进「其它」——因为真实命令都写成
`cd "D:/x" && dotnet build`，分类器看到的永远是 `cd`。剥掉前缀后：

| 分类 | 修前 | 修后 |
|---|---|---|
| 其它 | 5608 | 25（仅当天口径） |
| 脚本 | 0 | 98 |
| 查看 | 0 | 281 |

**2. `python -c` 会被误判成「装依赖」。** 内联代码体里常出现 `pip install`、`git`、`pytest` 字样，
所以「临时脚本」必须排在构建/测试/依赖**前面**，且只认 `-c` / `-e` / 标准输入三种形态——
`python -m pytest` 仍然归测试。这条有专门回归用例。

**3. 缓存字段差点被重复计一遍。** 实测 `total_tokens = input_tokens + output_tokens` 成立，
说明 `cache_read_input_tokens` **是 input 的子集**，不是额外量。第一版把它和 input 相加，
输入量直接虚高近一倍。现在只报「命中率」（实测 98–99%，长会话常态），
成本换算必须按 `全价部分 = input − cache_read` 拆。

### 这一轮的验收

| 项 | 结果 |
|---|---|
| 全部测试 | **40 tests OK**（22 → 40，新增 18 个） |
| 轮次与指令一一对应 | 三天共 7 个工作区，`prompts == len(turns)` 全部相符 ✔ |
| usage 是否重复计 | 连续 `function_call` 记录的 usage 无重复值，一调用一份 ✔ |
| `scan --limit 3` + 两次 `render` | 产出正常，md5 一致，**幂等未破坏** ✔ |
| 三平台语法 | 未用 3.9+ 语法（`removeprefix` / 内建泛型 / 联合类型），`requires-python >=3.8` 成立 ✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/adapters/base.py` | 新增 `EV_USAGE` / `usage_event()` / `tool_type()` / `cmd_class()` / `strip_cd_chain()` / `git_identity()` / `safe_lines()`；`extract_cmd` 剥 `cd` 前缀 |
| `scripts/adapters/workbuddy.py` | 发 `cmd`、`usage` 事件；读取改用 `safe_lines` |
| `scripts/adapters/claude_code.py` | 发 `cmd`、`usage`（按 `message.id` 去重，防同条消息多内容块重复计） |
| `scripts/archiver.py` | `DayCollector` 增加轮次、工具分类、命令分类、token 分桶；预览新增「动作构成 / 命令用途 / token 用量」三行与「无 token 记录」体检项 |
| `scripts/tests/test_daylog.py` | 新增 usage 收敛、usage 归属、内联脚本误判、命令分类等用例 |
| `README.md` `SKILL.md` | daylog 章节重写：五层语义、三段汇总、token 口径警示 |

> 数字口径说明：上一节的「22 tests」是第二轮之前的状态，当前为 **40 tests**；
> `archiver.py` 现约 2900 行（新增部分都在 `DayCollector` 与预览输出，归档主流程未动）。

---

## 八、第三轮：外部写入归属 + 落盘（第五、六节的待定项已实现）

两件之前挂着等决策的事，都按「默认不改动、显式开启/独立子命令」的方式落地了，都可回退。

### 1. `daylog --merge-external`：区外写入升为独立行

之前 11-16 那天 145 次写盘（69%）在表格之外，日志正文只能覆盖一半工作量。现在按仓库身份拆成独立行：

```text
工作区                                          会话  指令   写   改   工具   命令  跨度
C:/Users/me/WorkBuddy/2026-11-11-19-50-35       1    29    6    5    761    429  00:00-23:11
C:/Users/me/WorkBuddy/2026-11-16-20-46-15       1     2    1    0      3      1  20:46-20:48
↳ 区外 some-plugin                                 0     0    2    5      0      0  21:22-23:10
↳ 区外 tools                                       0     0    2    0      0      0  21:59-23:10
```

关键取舍：**伪行不分配 token**。usage 是模型调用级的，没有目录维度，硬拆就是编数字。
默认关闭（`daylog` 仍旧只统计+警示），因为「把区外当工作区」是个判断，不该替用户做。

### 2. `log`：清点稿落盘

```bash
python scripts/archiver.py log --date 2026-11-18            # → <target>/logs/2026-11-18.md
python scripts/archiver.py log --date 2026-11-18 --out ./logs
python scripts/archiver.py log --date 2026-11-18 --no-merge-external
```

结构固定五段：项目清点（含时间线：指令 → 命令 → 文件 → token）／当天合计／工作区外改动／数据缺口／生成方式。

**刻意不写「完成 / 进行中 / 阻塞」。** 那需要读懂上下文，属模型职责；用「有没有报错、有没有文件」
这类启发式硬猜，产出的会是「看起来像结论的错误结论」。清点稿只摆证据，归纳交给调用方——
这也是它敢承诺**不调模型、确定性输出**的前提。

三条硬约束：

| 约束 | 实现 | 破坏后果 |
|---|---|---|
| 幂等 | 渲染是纯函数，写前比对旧内容，相同则打印「无变化」 | 定时任务每天产生 diff，用户第一反应是卸载 |
| 脱敏 | 提示词过 `desensitize()`；区外路径命中 `.ssh`/`.aws`/`.env`/`*.pem` 只计数不列名 | 凭据位置被贴进日志系统 |
| 隔离 | 只写 `logs/`，不碰归档目录 | 无法回退 |

### 这一轮踩到的三个坑（都已修）

**1. 字段名撞车，把真工作区误标成区外。** 真实分组里 `external` 已经是「区外写入计数」这个 dict，
我又拿 `g.get("external")` 当布尔标志用——dict 非空即真，结果 11-11 那个正常会话被打了 `↳ 区外` 标记，
`统计口径` 同时被算错（写盘从 209 掉到 1）。改用独立字段 `is_ext`；
JSON 侧同理，`external`（计数）与 `external_row`（是否伪行）必须分开命名。

**2. 时间戳类型不一致，跨平台会崩。** Claude Code 的 `ts` 是 ISO 字符串，WorkBuddy 是毫秒整数，
两个源混排时比较报 `'>' not supported between 'int' and 'str'`。已在**适配器边界**统一成毫秒整数，
并让 `iso_to_ms` 接受纯数字字符串（部分平台把毫秒存成字符串）。这条契约写在 `base.py` 里，新适配器必须遵守。

**3. `.ssh/config` 差点写进日志。** 抓 11-14 的真实数据时发现区外改动里出现了 `.ssh/config`。
归档侧本来就有 `is_sensitive`，但日志是「给人看」的出口，路径本身就是信息（哪台机器、哪个云账号），
何况它还会被贴进群里。新增 `SENSITIVE_PATH_HINT` + `safe_path_label()`，命中即替换为「（敏感路径已隐藏）」，
只保留计数。

### 验收

| 项 | 结果 |
|---|---|
| 全部测试 | **49 tests OK**（40 → 49，新增区外合并、敏感路径、渲染确定性、落盘幂等、时间戳契约） |
| `log` 连跑两次 | 第二次打印「无变化：…」，md5 不变，**幂等通过** ✔ |
| 三天真实数据 | 11-14 / 11-16 / 11-18 均成功落盘（12.4KB / 6.6KB / 4.3KB） ✔ |
| `scan --limit 3` + 两次 `render` | md5 一致，**归档主流程未受影响** ✔ |
| JSON 兼容 | 新增 `external_row`，原 `external`（计数）保留，旧消费者不受影响 ✔ |
| CI | 增加 `log` 落盘 + 二次运行的幂等冒烟 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | `collect_day()` 抽出复用；`_merge_external_groups()`；`render_daylog_md()` / `cmd_log()`；`SENSITIVE_PATH_HINT` / `safe_path_label()`；`--merge-external` / `--no-merge-external` / `--out`；`log` 子命令 |
| `scripts/adapters/base.py` | 新增 `git_work_tree()`；`iso_to_ms` 接受数字字符串 |
| `scripts/adapters/workbuddy.py` `claude_code.py` | `EV_TS` 统一输出毫秒整数 |
| `scripts/tests/test_daylog.py` | 新增 9 个用例 |
| `.github/workflows/ci.yml` | 增加落盘幂等冒烟 |
| `README.md` `SKILL.md` | 新增 `log` 章节与三条硬约束；命令表补 3 行 |

行数：`archiver.py` 2946 → 3243，`base.py` 542 → 557，测试 40 → 49 个。

---

## 九、项目命名改为「证据链」+ 判定透明化（v0.2.0 第一项）

### 问题（实测暴露）

`scan --dry-run` 在 23 个项目单元里有 11 行是空壳或混合产物，且项目名出现
`https`、`密码为什么为空 InternetGatewa`。根因是 `_guess_name()` 只取会话标题，
**从不读磁盘上的身份证据**——而标题是「用户随口说的话」，不是「项目叫什么」。

### 改动

**1. 命名按证据强度重排**（`archiver.py`）

| 顺序 | 证据 | 实现 |
|---|---|---|
| 1 | 用户手改名 | `pick_override`（原有） |
| 2 | git 远端仓库名 | `git_remote_name()`：`git -C <ws> config --get remote.origin.url` |
| 3 | 工程清单 | `manifest_name()`：package.json / pyproject / Cargo / go.mod / `*.csproj`(AssemblyName) / `*.sln` |
| 4 | 工作区目录名 | 排除 `PLATFORM_DIR_PAT`（时间戳目录）与 `GENERIC_DIRS` |
| 5 | 会话标题 | 原有 `pick_title()`，降级为兜底 |
| 6 | 语言 | 最后兜底 |

新增 `identity_name()` 统一出口，返回 `(名字, 依据)`；`Project.name_src` 记录依据，
`refine_name()` 在名字已由身份确定时不再改写。

**2. 标题清洗三处修补**

| 坑 | 现象 | 修法 |
|---|---|---|
| 冒号切断 URL | `https://...` → 标题变成 `https` | `re.split(r"...\|:(?!//)")`，协议冒号不切 |
| 注释前缀未剥 | `// UTF8ToUnicode.cpp : 定义 DLL` | 前导字符集加入 `/\\`，切完得到文件名 |
| 句子当名字 | `密码为什么为空 InternetGatewa` | `SENTENCE_HINT`：长度 ≥14 且含疑问/请求词一律判为句子 |

**3. 三级兜底，避免全退回语言名**

`pick_title()` 现在：像名字的标题 → **链接的主机名**（`https://www.example.org/...` → `example.org`）
→ **剩下的句子截断**。最后一级是被迫的：一个能区分彼此的长句，好过三个都叫「Python 项目」。

**4. 空壳折叠 + 判定透明**

- `--dry-run` 默认隐藏空壳（工作区在但无源码），只留一行 `空壳 7 个未列入…加 --all 查看`；
  `--all` 展开。
- 新增 `--explain [关键字]`（零写入）：打印工作区、源码数/阈值、入口文件、**命名依据**、会话时间跨度。
- 表格改用 `_pad/_cut/_dw` 按终端显示宽度对齐（中文双宽不再撑歪）。

**5. `status` 增加 `.git` 体积体检**

超过 `GIT_WARN_BYTES`（200MB）提示 `git gc`，**不做自动 gc**——会拖长每日定时任务。

### 验收

| 项 | 结果 |
|---|---|
| 单元测试 | **83 tests OK**（49 → 83，新增 `test_naming.py` 34 个） |
| `https` / `密码为什么为空` | 归零 ✔ |
| `CDN`（三字母目录名） | 保留为 `CDN`，不再退化成「C# 项目」✔（回归用例覆盖） |
| 空壳折叠 | 23 行 → 16 行，7 个空壳收进一行说明 ✔ |
| `--explain Ledger` | 正确报出 `命名 BatchRenamer ← git remote origin` ✔ |
| 归档主流程 | 临时目录 `run --limit 3` 通过；二次 `scan` 对稳定项目报「无变化」；`render` 两次 md5 一致 ✔ |
| 脱敏 | git 远端 URL 中的 `user:token@` 先剜掉再取名（有专门用例）✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | `identity_name()` / `git_remote_name()` / `manifest_name()` / `_clean_identity()` / `_accept_identity()` / `url_host_name()`；`short_title` / `is_junk_title` / `pick_title` 重写；`_guess_name` / `refine_name` 改造；`explain_projects()`；`dir_bytes()` + `GIT_WARN_BYTES`；`run_scan` 增加 `show_all` / `explain`；`cmd_status` 体积体检；`--all` / `--explain` 参数 |
| `scripts/tests/test_naming.py` | 新增，34 个用例 |
| `README.md` | 命令表 +3 行；新增「🏷️ 项目名从哪来」章节 |
| `SKILL.md` | 命令表 +2 行，`status` 说明补体积体检 |

---

## 十、`find` 跨项目检索（v0.2.0 第二项）

### 问题

归档是个博物馆：东西都在，但得先知道在哪一格。真实场景是「上个月写过一个把批处理转
PowerShell 的小脚本，在哪来着」——旧做法是打开 `INDEX.md` → 27 个项目里猜 → 进去翻
`_meta/conversations.md`。没人会这么干，于是归档只有每月一次的打开频率。

### 改动

新增 `find <关键词>`（`cmd_find`），**零新增存储**，只读三处已有产物：

| 数据源 | 用途 |
|---|---|
| `index.json` | 项目名 / 目录名 / 用途 / 首末开发日期 |
| `source/` 实际文件树 | 文件名匹配（`walk_source`，不做 stat，单项目上限 4000） |
| `<项目>/_meta/conversations.md` | 指令原文匹配（`parse_conversations` 按行解析，比重新解析转录快两个数量级） |

**评分与命中语义**

| 命中位置 | 权重 | 说明 |
|---|---|---|
| 项目名 / 目录名 | 10 × token 数 | 用户记得的通常是名字 |
| 文件名 | 3 + min(6, n−1) | 数量**饱和**：2000 个备份文件不能压过名字命中 |
| 指令原文 | 3 + min(6, 会话数−1) | 每个会话最多贡献 1 条片段，避免同一项目刷屏 |
| 摘要 purpose | 1 | 模型写的，命中不代表真在这里干过 |

- **多词 = 全部命中**（AND），但允许跨字段组合：「报表」在名字、「插值」在指令，合起来才算。
- 排序：先按最后开发日期新→旧，再按分数稳排（同分时最近做过的在前）。
- 弱命中（仅命中摘要，分数 < 3）默认折叠，`--all` 展开。

**输出与开关**

- `--open`：打开第一条命中的原对话（取该条片段的深链；无深链时提示回场脚本路径）。
- `--json`：`{query, total, cost_s, hits[{name,dir,workspace,score,snippets}]}`。
- 退出码：有命中 0，无命中/用法错误 1（便于脚本判断）。

**安全**：`SENSITIVE_PATH_HINT` 命中的路径既不显示、也不参与检索命中——`.ssh/config`
不该因为某个查询被「命中」而暴露存在。

### 踩坑（两个，都来自测试）

1. **评分被文件数量撑爆**：第一版按「命中文件数 × 权重」累加，阅读器 项目 1000+ 备份文件
   直接算出 5156 分，排序失去意义。改为同类证据饱和（首项 3 分，每多一项 +1，上限 +6）。
2. **全部弱命中时崩溃**：`shown` 为空还去取 `shown[0]`，抛 IndexError。用户查一个只出现在
   摘要里的词就会踩到。已加空态返回，并有专门用例守着。

### 验收

| 项 | 结果 |
|---|---|
| 单元测试 | **100 tests OK**（83 → 100，新增 `test_find.py` 17 例） |
| 真实归档（8 项目，含 2200 文件的 阅读器） | `find "阅读器"` 0.21s；`find "插件"`、`find "内驱力"` 均正确 |
| 多词 AND | `find "workbuddy 日志"` 命中 1 条；`find "报表 PowerShell"` 正确判为无命中 |
| 排序 | 名字命中排文件命中之前；同分按最近开发时间 |
| 敏感路径 | `find "ssh"` 返回无命中，`.ssh` 不出现在输出里 ✔ |
| 无索引 / 空查询 | 给出可操作的提示，退出码 1 ✔ |
| 只读性 | 全程不加锁、不写文件、不调模型 ✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | 新增 `find_tokens` / `hit_count` / `snippet_of` / `parse_conversations` / `walk_source` / `open_session` / `cmd_find`；`FIND_W` / `FIND_CAP` / `FIND_MAX_FILES`；`find` 子命令 + `query` 位置参数 + `--open` |
| `scripts/tests/test_find.py` | 新增，17 个用例 |
| `README.md` | 30 秒看懂加第 3 步；命令表 +3 行；新增「🔍 找回做过的东西：find」章节 |
| `README.en.md` | 命令表 +5 行（find 3 行 + dry-run/explain 2 行） |
| `SKILL.md` | 何时使用 +2 条；命令表 +3 行 |

---

## 十一、提交信息生成器（v0.2.0 第三项）

### 问题

`git_init()` 每次提交都写死一句话：`sync: 同步 {name} 归档内容`（旧版 `:1710`、`:1725`）。
27 个仓库、每天一提交，`git log` 里全是一模一样的行——**账本一直在记，记的只是「今天有变化」**。
这让"每日定时 + git 产物"这两个本来成立的设计同时失效：git 历史本该是最值钱的东西。

### 数据来源：上次提交以来，而不是"今天"

用 `git log -1 --format=%ct` 取上次提交时间，只取该时间之后的对话指令。

理由：定时任务若在 00:05 跑，按自然日划界会把前一天晚上的活全漏掉；漏跑一天也无法自动补齐。
按「上次提交」划界两个问题一起解决，且不需要新存储。

### 主题按信息量挑，不按时间挑

第一版直接把**最新一条**指令当主题，真实数据立刻打脸：

| 项目 | 取最新一条（错） | 按信息量挑（实际采用） |
|---|---|---|
| CDN | `先不修改了。问题不影响使用` | `将首选字填入文本框后，如果继续等待的话…请检查问题` |
| MCP 日志 | `刚断网了，继续未完成的工作` | `重新分析这个dev-project-archivist 项目的功能完善方向` |
| Claw | `在e盘` | `帮我在电脑上找一下Wendy的作文` |
| 阅读器 | `F:/` | （全部被过滤 → 退回原文案） |

`subject_score()` 打分规则（确定性、无模型调用）：

| 信号 | 分值 | 依据 |
|---|---|---|
| 长度 10–40 | +2 | 一句完整诉求的典型长度 |
| 长度 >40（已被截断） | +1 | 交代得细的真任务，不能因为长就罚 |
| 长度 6–9 | −1 | 多半是半句 |
| 长度 <6 | −3 | 「好的」「继续」 |
| 以续做词开头（继续/还是/仍然/不用/在/是…） | −2 | 是上文的下半句 |
| 命中 `SUBJECT_META`（继续未完成/继续按计划/断网…） | −3 | 只说继续，没说继续什么 |
| 命中动作动词（修复/新增/重构/导入…） | +2 | 像"做完了一件事" |
| 像代码（`{}`、`#define`、`private `、`</…>`） | −4 | 是粘贴进来的代码，不是诉求 |

同分时取更新的那条（`max` 返回首个，列表最新在前）。

### 四条硬约束

| 约束 | 原因 |
|---|---|
| **先脱敏再截断** | 反过来的话 token 被截成半截，正好躲过 `SECRET_TOKEN_PAT` |
| **只取第一行** | 多行指令常贴着代码，进 commit 会让 `git log` 变噪声 |
| **提到敏感路径的整条丢弃** | commit 会进 git 历史；替换成「（已隐藏）」后语义变形（"改了 .ssh/config 的权限"→"改了（已隐藏）的权限"），不如不要 |
| **没有可用指令 → 退回 `同步 X 归档内容（N 项变化）`** | 宁可信息少，不可编造 |

另剥掉平台痕迹：`@skill:xxx`、`/clear`、纯路径（`F:/`）、纯链接。

### git-clean 的判据换成提交体标记

原判据是匹配中文文案前缀（`init: 归档` / `sync: 同步`），文案一改就失效。
现在提交体带 `X-Archiver: dev-project-archivist`，`archiver_commits_only()` 按标记判断；
旧提交按 `LEGACY_PREFIX` 兼容，升级后老仓库不会被误判成"含人工提交"。

### 踩坑（五个，全部来自测试与真实数据）

1. **先压空白再取首行 → 三行粘成一行**。`" ".join(t.split())` 会吃掉换行，多行指令里的代码全跟进来。顺序必须先切行再压空白。
2. **`len < 4` 判噪声误杀中文**。`新的活`（三字）已是完整语义，被当垃圾扔掉；下限改 3（`好`/`继续` 仍拦得住）。
3. **`SENSITIVE_PATH_HINT` 在自由文本里漏判**。它是按路径列表写的，要求 `.ssh` 在开头或紧跟 `/`；用户写「把 .ssh/config 权限改一下」时前面是空格。新增 `SENSITIVE_TEXT_HINT`，用否定环视匹配自由文本。
4. **截断反而被罚**。长指令截断后长度 41，落进 `else` 分支 −3，结果"交代得最细的那条"输给"继续未完成的工作"。改为对截断项 +1。
5. **`git-clean` 在 Windows 上一个仓库都重建不了**（旧 bug，本轮首次实跑暴露）：git 把 `.git/objects` 下的对象标成只读，`shutil.rmtree` 抛 `WinError 5`。新增 `_force_rmtree()`，`onerror` 里 chmod 后重试。

### 端到端实测（临时归档，真实会话）

```text
$ git log --format="%s" -1
sync: 重新分析这个dev-project-archivist 项目的功能完善方向，并给… 等 5 项

$ git log -1 --format=%b
- 继续按计划实施
- 不加。继续第二项
- 可以考虑：在 status 里加体积体检提示、让用户自己决定 gc
- 你分析有误
X-Archiver: dev-project-archivist
```

| 项 | 结果 |
|---|---|
| 单元测试 | **132 tests OK**（100 → 132，新增 `test_commit_msg.py` 32 例） |
| 首次归档 | `init: 归档 X` 不变，git-clean 前缀仍匹配 ✔ |
| 二次扫描无变化 | 提交数不变，不产生空提交 ✔ |
| `render` 提交 | 主题固定为 `更新 README 与索引`（render 没有转录可读，不假装知道"做了什么"）✔ |
| `git-clean` | 新格式提交被正确识别并重建（2/2）✔ |
| 敏感信息 | token / `.ssh` / `.env` 不入提交，有专门用例 ✔ |
| 兼容性 | 未用 3.9+ 语法；`daylog` / `log` / `find` 全绿 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | 新增 `commit_item` / `subject_score` / `sync_subject` / `git_last_commit_ts` / `recent_prompts` / `archiver_commits_only` / `_force_rmtree`；`git_init` 增加 `prompts` / `note` 参数；`cmd_git_clean` 改用标记判据并修复 Windows 只读删除；scan / render 两处调用点 |
| `scripts/tests/test_commit_msg.py` | 新增，32 个用例 |
| `README.md` | 新增「🧾 提交信息：那天到底做了什么」章节 |
| `SKILL.md` | 产出说明补提交信息规则 |

---

## 十二、跨天账本 `stats`（v0.2.0 第四项）

### 问题

`daylog` 只能看一天。但「这周到底干了多少」需要跨天，且新用户第一天没有任何归档产物时，
`find` / `INDEX.md` 都还是空的——**冷启动阶段唯一能立刻给出价值的就是 token 用量**。

### 先做重构：一次解析，按天分桶

原来 `collect_day` 是「单日 + 全量解析」，连跑七天会把 735MB 转录解析七遍。现在拆成三层：

| 函数 | 职责 |
|---|---|
| `_load_cols(sources, s_ms, e_ms)` | 解析范围内所有会话 → `(stat, cols, adapters)` |
| `_aggregate_day(cols, date)` | 按某一天归并成 `{ws -> group}` |
| `_merge_group(dst, src)` / `_finalize_groups` | 跨天合并与派生字段 |
| `collect_day(d)` | = `_load_cols` + `_aggregate_day(d)`，行为与改动前一致 |
| `collect_range(days)` | 一次解析 + 逐日聚合 + 合并总数，供 `stats`（将来还有 `report`）使用 |

`collect_day` 的对外行为没变：`daylog` 输出与改动前逐字一致（11-18：3 个会话、24 条指令、73.4M 输入）。

### 输出

```text
  最近 7 天 · 2026-11-13 → 2026-11-19

  对话 327 · 会话 6 · 项目 6 · 改动文件 172（写 155 / 改 17）
  Token 输入 936.0M（缓存命中 99%）· 输出 5.3M · 合计 941.4M
  成本：不估算（模型归属常为 auto，拿默认单价顶替等于编数字）

  每日对话  ▁▄█▂▂▂▁
  日期         对话   项目   文件    token
  ...
  最活跃项目
     276 对话 · 103 文件 ·  757.8M tok   熟悉阅读器桌面插件代码
```

- `--days N`（默认 7）、`--date` 作为截止日、`--json` 机器可读。
- 项目名：普通目录取目录名；平台时间戳目录（`2026-11-18-21-34-40`）取会话标题，
  否则一行全是时间戳，看不出是哪个项目。
- **不估成本**：只报 token。模型归属常为 `auto`，按默认单价算就是编数字。

### 口径校验（关键）

单日 `stats` 与 `daylog` 必须给出同一个数：

| 指标 | `daylog --date 2026-11-18` | `stats --days 1 --date 2026-11-18` |
|---|---|---|
| 对话 | 24（17+5+2） | 24 ✔ |
| 输入 token | 73.38M | 73.4M ✔ |
| 输出 token | 416.8k | 416.8k ✔ |

### 踩坑：cache_read 又算重了一次

第一版每日合计与项目合计写成 `sum(g["tok"].values())`，把 `in + out + cache_read` 全加了。
实测每日合计 1868M、总数 941M，**差整整一倍**——因为 `cache_read` 是 `input` 的子集
（第 5 轮 daylog 阶段已经踩过，这次换了个地方又踩一遍）。

修法：抽出 `tok_total(g) = in + out`，注释写在函数里，并加两条回归用例。
现在每日合计与总数严格相等（941.4M）。

### 验收

| 项 | 结果 |
|---|---|
| 单元测试 | **148 tests OK**（132 → 148，新增 `test_stats.py` 16 例） |
| 口径一致 | 单日 `stats` 与 `daylog` 的对话数/token 完全一致 ✔ |
| 每日合计 = 总数 | 941.4M == 941.4M ✔ |
| 对齐 | 表头与数据行显示宽度相等（有专门用例，中文双宽） |
| 空白天 | 列出日期，不额外加标记列（加过一版会让表格缺一角） |
| `daylog` 回归 | 输出与改动前一致 ✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | 拆出 `_load_cols` / `_aggregate_day` / `_merge_group` / `_finalize_groups` / `_empty_group`；新增 `collect_range` / `tok_total` / `human_num` / `_day_list` / `_spark` / `_group_label` / `cmd_stats`；`stats` 子命令 + `--days` |
| `scripts/tests/test_stats.py` | 新增，16 个用例 |
| `README.md` | 命令表 +3 行；新增「📊 这周干了多少：stats」章节 |
| `SKILL.md` | 触发场景 +1 条；命令表 +1 行 |

---

## 十三、单文件 HTML 账本 `report`（v0.2.0 第五项）

### 为什么需要它

`stats` 在终端里好用，但**终端截图发不出去**——贴到群里是一堆等宽字符，发到朋友圈没人放大看。
而"这月我让 AI 改了多少东西"本身是个值得展示的事实。`report` 就是把它做成一张能直接发的纸。

### 两条设计红线

| 红线 | 落实方式 |
|---|---|
| **能分享 ≠ 敢分享** | 默认不含工作区绝对路径（含用户名、内网目录）、不含指令原文（可能含代码、密钥、私事） |
| **零依赖** | 单文件、无 `<script>`、无外链样式/字体、无远程图片；唯一外链是页脚仓库地址（纯文本链接，不加载资源） |

项目名走与 `--dry-run` 同一套证据链，再过 `desensitize()`：

| 输入（目录名） | 报告里显示 |
|---|---|
| `my-project` | `my-project` |
| `2026-11-18-21-34-40`（平台目录） | 会话标题，如 `重构归档器` |
| `.ssh` / `.aws` | **（名称含敏感信息，已隐藏）** |
| `proj-access_token=abc123` | `proj-access_token=<REDACTED>` |

刻意没有做宽 `desensitize` 去认裸 `token`——它在提示词里是高频正常词（"token 用量"），
一律抹掉会把正文改烂。这条边界第 5 轮就定过，这里沿用。

### 页面结构

| 区块 | 内容 | 为什么 |
|---|---|---|
| 四张总览卡 | 对话 / 项目 / 改动文件 / token | 一眼看完的量级 |
| 日历热力图 + 三条事实 | 最忙的一天、活跃天数、平均每天 | 热力图只说"哪几天忙"，说不出"忙到什么程度" |
| 每日柱状图 | 内联 SVG，每根柱带 `title` | 高度是数据，不能让布局参与计算（用 div 会随字体变化） |
| 项目卡片 | 名字 / 对话数 / 进度条 / 会话·文件·token·起止 | 按对话量排序，卡片上限 40（再多就是另一个 INDEX.md） |

- 浅色深色跟系统 `prefers-color-scheme` 走；窄屏 ≤560px 自动单列。
- 文件名只由区间决定：`report-2026-10-21_2026-11-19.html`；页面里**没有**"生成于 12:34:56"，
  所以同一区间重复跑是逐字节相同的（否则每天多一个文件）。

### 实现中调整的两处

1. **短区间不画热力图**。第一版三天区间也画，结果是一个 17px 的格子在左边、三行事实吊在右边，
   看着像页面坏了。改为 `len(daily) < REPORT_HEAT_MIN_DAYS(10)` 时改排三张横排事实卡。
2. **单日报告里 "0 token" 改成 "Token 未记录"**，与 `stats` 的文案统一。

### 验证（真实数据 + 三种宽度）

| 项 | 结果 |
|---|---|
| 单元测试 | **179 tests OK**（148 → 179，新增 `test_report.py` 30 例） |
| 30 天真实数据 | 9 个项目 / 383 条对话 / 1.1B token，页面完整 |
| 1 天、3 天 | 走事实卡分支，无热力图 ✔ |
| 空区间（2020 年） | 正常产出，显示"这段时间没有记录到开发活动" ✔ |
| 暗色模式 | 文字与图表对比度正常（`--force-dark-mode` 实截）✔ |
| 窄屏 481px | 无横向溢出（`scrollWidth == clientWidth`，注入脚本量过）✔ |
| 路径泄漏 | `grep` 检查 `C:/`、`/home/`、`Users/` 均为 0 ✔ |
| 外链 | 全文只有一个 `https://github.com/winrich/dev-project-archivist` ✔ |
| 幂等 | 固定区间连跑两次 md5 相同（`76db7bb…`）✔ |
| 退出码 | 坏日期 1、正常 0 ✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | 新增 `report_label` / `_rep` / `_heat_level` / `_rep_heat` / `_rep_bars` / `_rep_facts` / `_rep_projects` / `report_data` / `build_report_html` / `cmd_report` / `REPORT_CSS` / `REPORT_PROJECTS_MAX` / `REPORT_HEAT_MIN_DAYS`；`report` 子命令；`--days` 默认值改为按命令取（stats 7 / report 30）；`--out`、`--open` 帮助文本 |
| `scripts/tests/test_report.py` | 新增，30 个用例 |
| `README.md` | 30 秒看懂加第 4 步；命令表 +3 行；新增「🖼️ 把它发给别人：report」章节 |
| `README.en.md` | 命令表 +2 行（stats / report） |
| `SKILL.md` | 触发场景 +1 条；命令表 +1 行 |

---

## 十四、按日恢复 `restore`（v0.2.0 第六项）

前五项都是「把账本看懂」，这一项是「把账本用起来」：既然每个项目目录都是一个独立仓库、
每次扫描一次提交，那么「11 月 14 号那天归档里是什么样」就该是能精确回答的问题，
而不是靠在几十个仓库里手动 `git log`。

### 先定语义，再写代码

这条命令最容易做错的不是技术，是**语义含糊**——用户一句「回到那天」里其实至少混着三种期待：

| 用户可能的意思 | 本命令是否回答 |
|---|---|
| 「那天归档里有什么」 | ✅ 就是这件事 |
| 「那天改了什么」 | ❌ 那是 `log` / `stats` / `git diff` |
| 「把我的东西恢复成那天的样子」（就地覆盖） | ❌ 明确不做，只出副本 |

所以实现前先把三条语义写死在注释、README、SKILL 三处（三处文案同源，改一处必须同步另两处）：

1. **只从归档仓库自己的 git 历史取**。原工作区可能早被删/改名/被下一次对话覆盖，不是可靠来源。
2. **取的是「该日结束时」最近一次提交**，不是「当日恰好有提交」。某天没跑归档 → 退到更早的最近一次，
   并把那个提交的真实时间打在界面上，**不假装是当天**。
3. **只包含当时已归档的文件**。媒体、超大二进制、敏感路径、`.gitignore` 排除的构建产物、
   以及首次归档之前的任何文件，从来没进过 git——凭空变不出来，所以不当备份卖。

### 关键实现选择

| 选择 | 备选 | 为什么选它 |
|---|---|---|
| `git archive` → tar → `tarfile` 逐成员解包 | `git checkout -- .` / `git worktree add` | checkout 会动工作区和索引，可能把归档搞成 detached HEAD、覆盖未提交的手工改动；`git archive` 只读对象库 |
| 自己比 Unix 秒，不用 `git log --until=<date>` | `--until` | 后者按提交里的时区偏移解释边界；本机 +0800、git 内部存 UTC，边界那次提交会被判到隔壁天（已加用例 `test_boundary_uses_local_day_not_utc`） |
| 默认只预览，`--write` 才落盘 | 直接写 | 一次恢复可能几百 MB，先看清"取到哪一版、多少文件"再决定，比事后删目录便宜 |
| 写入点固定 `<target>/_restore/<date>/<项目>/` | 就地覆盖 | 「回到某天」最典型的翻车方式就是覆盖当前归档；这里从根上不给机会 |
| 解包时逐个成员拒 `..` 与绝对路径 | 直接 `extractall` | 恢复是「把旧数据写到现在」，路径穿越这一层不靠上游保证 |
| 同日再写默认拒绝，`--force` 才覆盖 | 静默覆盖 | 用户可能已经在副本上改过东西 |
| 总量 > 800MB 需 `--force` | 无上限 | 一个 `restore` 把磁盘塞满是没人想要的意外 |

### 输出

不写 `--date` 时先回答「能回到哪些天」——让用户去 27 个仓库各跑一次 `git log` 是不合理的：

```text
  可恢复的日期（各归档仓库有提交的日子，新 → 旧）

    2026-11-18    27 个仓库有提交 ·   27 次提交
    2026-11-17    25 个仓库有提交 ·   26 次提交
```

预览（零写入）：

```text
  归档快照 2026-11-14（当天 23:59:59 之前最后一次提交）
  预览，未写任何文件

  项目                          快照时间             文件      体积
  ------------------------------------------------------------------
  BatchRenamer                    11-14 22:03            312    12.4 MB

  小计 24 个项目 · 3180 个文件 · 96.2 MB

  无法恢复 3 个
    InvoiceParser         该日之前没有归档记录（首次归档 2026-11-16）
```

`--write` 后，恢复目录里附 `_restore.md` + `_restore.json`：每个项目取自哪个 commit、
各自文件数与体积，事后能逐条对账。副本不带 `.git`——它是给人打开/编译的，不是又一个仓库。

### 踩坑

| # | 问题 | 修法 |
|---|---|---|
| 1 | 空仓库（`git init` 无提交）返回 `{}` 而"有提交但都在目标日之后"返回 `{"sha": ""}`，两者提示语不同，混用会给出错误理由 | 用 `not c` 与 `not c["sha"]` 两级判定，用例分别锁住 |
| 2 | `--only` 过滤到一个都没有时，提示语仍是"该日之前没有归档快照"，把用户的过滤条件当成事实 | 先把 `kw` 分支拆出来，报「没有项目匹配 --only xxx」 |
| 3 | 一个都恢复不了还 `--write`，会在磁盘上留一个只有 `_restore.md` 的日期目录，下次进来的人会以为"那天归档过" | `rows` 为空时提前返回 1，不建目录 |
| 4 | `dict \| dict` 合并写法是 Python 3.9+，项目声明 `>=3.8` | 直接输出 rows（顺带少一层无用拷贝） |

### 验证

| 项 | 结果 |
|---|---|
| 单元测试 | **204 tests OK**（179 → 204，新增 `test_restore.py` 25 例，全部基于临时 git 仓库，不依赖真实数据） |
| 语义 | 11-10 / 11-14 两次提交的仓库：回到 11-12 拿到 11-10 的内容且时间显示 11-10；回到 11-14 拿到 b.py 而 11-10 没有 b.py ✔ |
| 不写回归档 | 恢复后归档工作区文件字节不变、`git status --porcelain` 为空 ✔ |
| 副本形态 | 不含 `.git`，`_restore.md` / `_restore.json` 齐全，记录的 commit 与 `git log` 一致 ✔ |
| 边界 | 早于首次归档返回 1 且不建目录；`--only` 无匹配有专门提示；同日二次写入返回 2，`--force` 后 0 ✔ |

### 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/archiver.py` | 新增 `RESTORE_DIR` / `RESTORE_MAX_MB` / `_git_run` / `git_commit_at` / `git_subject` / `git_tree_files` / `git_export` / `archive_dirs` / `restore_dates` / `cmd_restore` / `build_restore_index`；`restore` 子命令；`--write`；`--date` / `--out` 帮助文本 |
| `scripts/tests/test_restore.py` | 新增，25 个用例 |
| `README.md` | 30 秒看懂加第 5 步；命令表 +5 行；输出结构加 `_restore/`；新增「⏪ 回到某天：restore」章节（三条语义表 + 参数表 + 不覆盖归档的说明） |
| `README.en.md` | 30 秒加第 3 步；命令表 +5 行；输出结构加 `_restore/`；新增 "Going back to a given day" 章节 |
| `SKILL.md` | 触发场景 +1 条；命令表 +4 行；新增「按日恢复（restore）」小节（语义表）；输出结构加 `_restore/` |

---

## 十五、发行口径修正：版本号从 0.2.0 改为 0.3.0

### 问题

计划里 `v0.2.0 / v0.3.0 / v0.4.0` 是**阶段编号**（P0/P1/P2）。我在排发布时把阶段名直接当成了版本号，
于是出现两个方向都不对的候选：

| 候选 | 问题 |
|---|---|
| 0.2.0 | **漏报**：P1「账本视图」的 stats / report / restore 三项已经全做完，数字停在 P0 等于把它们藏起来 |
| 0.4.0 | **虚报**：P2「闭环与扩散」（批量推送、适配器扩容、首页素材）一项没动 |

### 定的规则

**版本号 = 计划中已完成档位的最高一个**，不提前、不落后。阶段名归计划文档，版本号归产物。

按此：P0 三项（命名证据链 / `find` / 提交信息）+ P1 三项（`stats` / `report` / `restore`）一起交付 →
首个公开版本 **0.3.0**。不补一个没人见过的 0.2.0——`pyproject` 里躺着的 0.1.0 从未发布、无 tag、无 Release，
凭空多一档只会让读者以为漏了一版。`CHANGELOG.md` 开头已把这件事写清，免得三个月后又有人问。

### 落地改动

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | `version` 0.1.0 → **0.3.0**（此前从未 bump） |
| `scripts/archiver.py` | 新增 `__version__`；`--version` 参数（`archiver.py --version` → `dev-project-archivist 0.3.0`） |
| `CHANGELOG.md` | **新增**：0.3.0 条目（新增命令 / 改进 / 修复 / 升级提示 / 质量）+ 0.1.0 基线 + 「为什么没有 0.2.0」 |
| `scripts/tests/test_release.py` | **新增** 4 例：`pyproject` / `__version__` / `CHANGELOG` 首条目三处必须同数，`--version` 可用 |
| `.github/workflows/ci.yml` | 见下方「顺带发现的 CI 缺陷」 |
| `README.md` / `README.en.md` | 徽章区加 Release 徽章（发布后自动显示最新 tag） |
| `完善计划-…md` | 开头加版本口径说明；Release / 提交拆分 / 验收门槛三处改为实际版本与实际结果 |

### 顺带发现的 CI 缺陷（比版本号更值得修）

`ci.yml` 只显式跑了 `test_adapters.py` 与 `test_daylog.py` 两个文件——**其余 5 个测试文件写了但 CI 从没执行过**，
本轮新增的 `test_restore.py` / `test_release.py` 同样不会被执行。「写了测试」和「测试在跑」是两件事，
逐个文件列举迟早会漏，因此改为 `python -m unittest discover -s scripts/tests -v`，
并补充 `stats` / `find` / `restore` 的空环境冒烟（只验不崩、不写文件）。

空环境验证：`HOME`/`USERPROFILE` 指向临时目录时 `stats --days 1`、`daylog` 均退出 0，
`restore --date 2020-01-01` 退出 1（无快照，设计如此，CI 里用 `|| true`）。

### 还没做的（不能算进 0.3.0）

- **README 首屏重排**：已做一半——「30 秒看懂」按频率拆成「第一次用（一次性）」与「之后每天用（只读）」两组，
  `find` / `stats` / `report` / `restore` 不再排在 `run` 之后；标题下的一句话与首屏叙事仍是「归档」口径，
  要不要改成「账本」口径需要先定调（这是转化率最大的单点，不宜擅自改）。
- **GitHub 侧动作**：打 tag `v0.3.0`、发 Release（正文用 `CHANGELOG.md` 0.3.0 段）、补 topics、设 homepage。
  本地工作副本没有 `.git`（也没有 `gh`），这些只能在网页或真实克隆里做。

---

## 十六、公开内容清洗：不出现竞品名称与本地真实项目名

### 为什么必须做

README 与测试里的示例输出，原本是**从真实归档里直接抄的**——项目名、工作区路径、
会话标题原样搬进文档。推上 GitHub 等于把本机目录结构和自己在做什么一并公开；
而"对齐某产品"这类措辞写在文档里，也容易被读成跟随者叙事。两件事都不该留在仓库里。

### 做法：统一虚构名，而不是逐处打码

打码（`XxxProcessor`）读起来像事故通报；换成**一套固定的虚构名**，示例仍然可信、可复现。
今后所有新增示例都用这一套：

| 用途 | 虚构名 |
|---|---|
| 桌面端 C#/WPF 类项目 | `BatchRenamer` |
| git 远端仓库名 / 包名示例 | `order-sync`（`@scope/order-sync`、`github.com/example/order-sync`） |
| 图像/流水线类项目 | `InvoiceParser` |
| 短名（三字母目录）回归 | `CDN`（候补 `QMS` / `OCR`） |
| 域名词退化示例 | `example.org` |
| 用户名占位 | `me` / `x` / `u` / `someone` |
| 外部目录示例 | `some-plugin` / `reader-private` |
| 办公类项目（find 示例） | `报表导出器` |

### 改到哪

| 位置 | 内容 |
|---|---|
| `README.md` | 命名证据链表、`--explain` 示例输出、`find` 示例输出、`stats` 项目卡、`daylog` 区外路径、`cd <归档目录>/…`、`find` 的示例检索词 |
| `scripts/archiver.py` | 两处注释里的示例站点与示例检索词（统一换成 `example.org` 与办公类示例） |
| `scripts/adapters/base.py` | 注释里示例路径的用户名段由 `xx` 改为占位名 `me` |
| `scripts/tests/*` | `test_naming` / `test_find` / `test_stats` / `test_report` / `test_daylog` / `test_commit_msg` 的夹具与断言同步改名（断言值一起改，不是只改输入） |
| 五份过程文档 | 竞品名统一为「对标产品 / 同类工具」，真实项目名同步换成上表虚构名 |

`LICENSE` 的版权人与 README 徽章里的 `winrich/dev-project-archivist` 保留——那是本仓库自己的身份。

### 踩到的两个坑

| # | 问题 | 修法 |
|---|---|---|
| 1 | 只改输入不改断言，改完一堆测试红 | 一次替换输入与期望值（同一套名字），再跑一遍验证语义没变 |
| 2 | `test_daylog` 的重复指令夹具：新文案只有 18 字，低于压重阈值 20，去重用例失效 | 换成长句（+「顺便补一条 CI」），保持"长指令才压重"的原意 |
| 3 | 守护测试自己写着竞品名，`grep` 全仓库时唯一命中的就是它 | 正则拆成 `"spec" + "story"` 拼接；测试文件自身也排除在扫描范围外 |

### 加了一道防线

新增 `scripts/tests/test_public_content.py`（2 例）：

- 仓库内任何文本文件都不许出现竞品名（正则拼出来，文件本身不含该字样）
- 不许出现 Windows 主目录形态的绝对路径（`C:` + `Users` + 真实用户名三段），占位名在允许表内除外（`me` / `x` / `u` / `someone`…）

不列"真实项目名黑名单"——黑名单本身会把要藏的名字刻在门上。这类东西靠**示例统一用虚构名**保证，
结构性检查兜住最容易手滑的两处。

### 结果

| 项 | 结果 |
|---|---|
| 全仓库检索（含二进制） | 竞品名 / 真实项目名 / 主目录真实用户名 **命中 0** |
| 单元测试 | **210 tests OK**（新增 `test_public_content` 2 例） |
| 语义未变 | 命名证据链、`find` 评分、`daylog` 压重等用例仅换文案，断言的**行为**不变 |

---

## 十七、入库与首推

### 仓库形态

沿用既有仓库 `winrich/dev-project-archivist`，由 public 改为 private，0.3.0 以一个提交接在原有 8 条历史之上
（未做 force push，旧提交全部保留）。已核对旧 8 条提交不含真实项目名，将来要转公开时历史是干净的。

本地工作副本原本没有 `.git`，因此：`git init -b main` → 首次提交 → `git fetch origin` → `git reset --soft origin/main`
→ 重新提交。这样得到的提交父节点是远程 HEAD，历史连续而不是两段无关历史。

### 三份过程文档入库

放在 `docs/`，附一个索引：方向再评估（诊断）、完善计划（施工单）、变更说明（落地记录）。
入库理由不是"留档好看"，是**将来能回看判断依据**——为什么砍掉某个方向、某个语义为什么写死、某个坑当初怎么踩的。

### 踩坑

| # | 问题 | 修法 |
|---|---|---|
| 1 | 文档描述"禁止主目录绝对路径"时把这个形态原样写了进去，`test_public_content` 把自己人拦了（3 处命中） | 改成描述性措辞（"`C:` + `Users` + 真实用户名三段"），不在文档里复制被禁的字面形态 |
| 2 | 本机 HTTPS 到 github.com 被代理拦（`CONNECT tunnel failed, response 502`） | 全程走 SSH（`git@github.com:`），密钥已就绪 |
| 3 | 本机没有 `gh`，也没有可用的 API token，建仓库/改可见性做不了 | 由仓库所有者在网页改可见性；推送本身只需 SSH，不需要 API |

### 结果

| 项 | 结果 |
|---|---|
| 远程 HEAD | `eefbdcc`，与本地一致；跟踪 `origin/main` |
| 远程文件 | 32 个（28 个项目文件 + 4 个 docs 文件） |
| 测试 | 210 tests OK（推送后重跑） |
| 版本 | `archiver.py --version` → 0.3.0 |
