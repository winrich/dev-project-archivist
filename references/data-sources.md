# 本机数据源实测规格（2026-11-13 核实）

这些是本机实际存在的路径与格式，改动解析逻辑前请先回归验证。

## 1. 会话数据库

`~/.workbuddy/workbuddy.db`（SQLite，WAL 模式）

关键表 `sessions`：`id / cwd / user_id / title / custom_title / status / created_at /
updated_at / deleted_at / is_playground / model / is_background_automation ...`

- 时间字段是**毫秒**时间戳。
- 归档器用只读 URI 连接：`file:<path>?mode=ro`；WAL 存在时若被占用会失败，已做降级。
- 实测 224 行有效会话（`deleted_at IS NULL`）。

## 2. 对话转录

`~/.workbuddy/projects/<cwd编码>/<sessionId>.jsonl`

- **文件名（去掉 .jsonl）与 `sessions.id` 一一对应**，这是最可靠的关联方式。
  目录名是 cwd 的编码结果，规则不稳定（出现过 `D-WorkBuddyData-WorkBuddy-...` 这类历史遗留），
  因此归档器**按文件名建立 sid → path 映射**，不反解目录名。
- 实测 226 份，合计 735.8 MB，单份最大 35,117 行。
- 每行一个 JSON 记录，`type` 取值：
  - `message`：`role` + `content[]`，item 类型有 `input_text` / `output_text` / `image_blob_ref`
  - `function_call`：`name` + `arguments`（**不是** content 里的 `tool_use`）
  - `function_call_result`：工具输出
  - `reasoning`：思维链，体积最大，**必须跳过**
  - `file-history-snapshot` / `ai-title` / `session-meta` / `resend-fork-notice`
- 用户消息的 `input_text` 会先塞入 `<system-reminder>`、`<user_info>`、`<identity_context>`
  等注入块，真实提问在 `<user_query>` 标签内，清洗时优先取该标签内容。
- 正在写入的会话尾部可能是半行，逐行解析失败即跳过。

## 3. 工具调用统计（单份最大转录样本）

`Bash 4030 / Edit 2042 / Read 1922 / Write 771 / Grep 447 / present_files 184`。
全库 `Write`+`Edit` 合计 10,047 次，202/226 个会话含代码写入。

`Write` 的 `arguments` 含 `file_path` + `content`，可用于**恢复磁盘上已删除的文件**；
`Edit` 只有 `old_string`/`new_string`，无法单独重建全文，故恢复只用 `Write`。

## 4. 产物索引

`~/.workbuddy/artifact-index/<sessionId>.json`，实测 143 份。
含 `artifacts[]`：`name/title/mimeType/contentType/size/uri/sourceTool/ownerConversationId`。
`uri` 指向真实文件（多数在 `~/WorkBuddy/<workspace>/output/...`）。

## 5. 文件版本快照

`~/.workbuddy/file-history/<sessionId>/<hash>@v<N>`，实测 172 个会话目录。
文件名是内容哈希 + 版本号，文件内容即该版本的文件文本。可用于回溯某文件的历史版本。

## 6. 工作区

`~/WorkBuddy/<workspace>/`，实测 95 个目录、6.4 GB。

- 26 个代码项目 / 52 个文档任务 / 17 个混合或空壳（按扩展名启发式统计）。
- 部分目录含 `node_modules`；个别目录达数 GB（含模型与二进制）。
- 目录内 `.workbuddy/memory/*.md` 是项目记忆与每日日志，**归纳素材价值很高**，
  归档时复制到 `_meta/memory/`。
- 同一 workspace 可承载多个会话（如英语晨间/晚间提醒同属一个学习应用）。

## 7. 踩坑记录

| 坑 | 现象 | 处置 |
|---|---|---|
| WAL 只读连接 | 应用运行中可能拿不到锁 | try/except 降级为纯文件扫描 |
| `\r\n` 混入会话标题 | 写进 Markdown 表格破坏行结构，读回时换行被归一化，导致渲染永不收敛 | 统一走 `md_cell()` 清洗 |
| `.workbuddy` 被忽略规则排除 | 又会被「从转录恢复」逻辑当成已删文件复活，与 `_meta/memory` 重复 | 恢复时显式跳过 `.workbuddy/` 前缀 |
| 渲染时用「当前时间」做归档更新时间 | 每次重跑都判定为变化，全量重写 | 改用 state 中记录的 `last_archived` |
| 中文路径下用 shell 删目录 | 会走安全删除并 fail-closed | 一律用 Python 的 `shutil.rmtree` |
| 正则里 `(?i)` 写在中段 | `re.PatternError: global flags not at the start` | 拆成两个模式或使用 `re.I` 参数 |
| `--force` 重算目录名 | 会留下旧目录形成重复 | 目录名一旦分配即保持，改名只由 render 按摘要标题做一次 |
