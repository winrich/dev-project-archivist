# 夜间自动化提示词（供 automation 使用，self-contained）

把下面整段作为定时任务的 prompt。它不依赖任何一次具体对话的上下文，只依赖脚本与目标目录。

**使用前请替换两处占位**：
- `<技能包目录>` → 技能包实际所在路径，默认安装位置为 `~/.workbuddy/skills/dev-project-archivist`
- `<归档目录>` → 你希望输出到的目录。若省略，脚本默认写到 `~/Workbuddy项目归档`；也可设置环境变量 `ARCHIVER_TARGET` 或用 `--target` 参数指定

---

执行 WorkBuddy 对话项目归档的每日同步。归档器脚本与目标目录如下：

- 脚本：`<技能包目录>/scripts/archiver.py`
- 目标：`<归档目录>`

按顺序执行，全程不要向用户提问。

**第 1 步 采集**
运行 `python "<技能包目录>/scripts/archiver.py" scan`，
读取输出末尾的「待归纳 N」。若 N 为 0，直接跳到第 3 步。

**第 2 步 归纳（只处理有变化的项目）**
运行同一脚本的 `pending` 子命令，拿到待归纳 `_evidence/evidence.md` 的绝对路径列表。
逐个读取证据包后归纳，写入同目录 `_summary.json`，字段固定为：
`title / background / purpose / features / tech_impl / tech_stack / language / highlights / pitfalls / status / confidence / source`。

字段要求：
- `title`：不超过 14 个汉字，必须是「主题 + 类别名词」，类别落在 工具 / 软件 / 系统 / 平台 / 插件 / 类库 / 上位机 / 脚本 / 生成器 / 助手 之一；不得以「优化 / 分析 / 逆向 / 扩展 / 方案 / 类 / 环境」结尾；不带日期与引号。
- `background`：1-2 句，写「为什么做、要解决什么痛点」，不写技术栈，60-90 字。
- `purpose`：1-2 句，写「做什么、谁在用」，40-70 字。
- `features`：必须分组，格式 `[{"module": "模块名", "items": ["功能点"]}]`，2-5 个模块，每模块 2-5 条，每条不超过 28 字，用「动词+对象」短语，按实际操作顺序排列。
- `tech_impl`：150-300 字，架构、关键模块职责、关键算法或第三方库、数据流。
- `tech_stack`：3-8 条；`language`：1-2 个，用 ' / ' 分隔。
- `highlights`：3-6 条；`pitfalls`：2-6 条，证据没有就给空数组。
- `status`：可用 / 原型 / 半成品 / 已废弃 四选一。
- `confidence`：0 到 1 的数字。

约束：
- 只依据证据包与 `source/` 文件清单，禁止编造证据中不存在的功能、库或结论；
- 证据不足时降低 `confidence`，并在 `background` 或 `purpose` 中注明「记录较少」；
- 若证据显示只是咨询/方案讨论、未产出可运行软件，`status` 填「原型」或「已废弃」并说明；
- 严格合法 JSON，无注释、无尾逗号、无 markdown 包裹；
- 除 `_summary.json` 外不得修改任何文件。

**第 3 步 渲染**
运行同一脚本的 `render` 子命令。这会按摘要标题重命名目录、重写 README、刷新总索引。

**第 4 步 汇报**
用不超过 8 行中文汇报：本次新增/更新/失联各几个、实际归纳了几个项目、归档总体积、索引文件路径。
若第 2 步因证据异常失败，如实说明失败的项目名，不要伪造摘要。

---

## 成本控制说明

- 稳定期 `pending` 为空 → 大模型调用为 0，只剩两次脚本执行（约 10 秒）。
- 只有指纹变化（工作区文件、会话轮次、会话时间发生变化）的项目才进 `pending`。
- 建议排在凌晨低峰，并把该任务绑到低价模型；脚本本身不消耗模型额度。
- 若某天 `pending` 异常多（例如首次运行或大改规则），可先 `scan --limit 20` 分批消化。

## 归档质量自检（可选，偶尔执行）

```bash
# 各项目体积，确认没有异常膨胀（把 <归档目录> 换成实际路径）
du -sm "<归档目录>"/*/ | sort -rn | head

# 确认每个项目都有 .git 与完整性报告
for d in "<归档目录>"/*/; do
  [ -d "$d/.git" ] || echo "缺 git: $d"
  [ -f "$d/_meta/integrity.md" ] || echo "缺完整性报告: $d"
done
```

发现某项目体积异常（如超过 20 MB），检查 `manifest.json` 的 `excluded.external` 与 `source/` 里的大文件；
若某个核心脚本被误剔，在 `overrides.json` 加 `{"keep_scripts": ["文件名.py"]}` 后重跑 `scan --force`。
