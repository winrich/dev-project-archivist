#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器基类与中立事件规范。

主程序（archiver.py）只认这里定义的 7 种事件，不认任何平台的 JSON 结构。
新增一个平台 = 新增一个 Adapter 子类 + 在 __init__.py 注册，主程序零改动。

事件规范（均为 dict，kind 为必填项）：

    {"kind": "ts",          "value": "2026-11-13T10:42:01Z"}   时间戳
    {"kind": "title",       "text": "会话标题"}                  会话标题
    {"kind": "prompt",      "text": "用户真实提问"}              已剥离注入块的用户提问
    {"kind": "assistant",   "text": "助手输出文本"}              助手正文
    {"kind": "tool",        "name": "Write", "cmd": ""}         工具调用。name 为平台原名，
                                                             cmd 仅在 shell 类工具上出现，
                                                             只放首行命令（≤200 字），用于回答
                                                             「今天跑了哪些构建/测试」。
    {"kind": "write",       "rel": "src/a.cs", "op": "write", "content": "…"}
                                                             归一为相对路径的写盘动作。
                                                             op=write 时 content 为文件正文；
                                                             op=edit 时 content 为空串，只表达「改了哪个文件」。
    {"kind": "tool_result", "text": "错误线索片段"}              工具输出中的错误线索（非全量）
    {"kind": "usage",       "in": 35959, "out": 132, "cache_read": 7680}
                                                             一次模型调用的 token 用量。
                                                             这是唯一能把「花了多少钱」和
                                                             「产出了什么」连起来的字段。

为什么 edit 也要发 write 事件：只统计 Write 会漏掉绝大多数修改，
「今天改了哪些文件」这类按天聚合会严重偏低。content 留空是为了不把
编辑正文带进内存——归档侧只关心 Write 的完整正文。

设计要点：
  1. adapter 负责「平台语义 → 通用语义」：字段名映射、路径相对化、注入块剥离都在 adapter 内完成。
  2. adapter 不做任何统计与判定，只吐事件；所有业务逻辑留在主程序的 SessionParser。
  3. tool_result 只保留错误线索，避免把整段工具输出带进内存（体积控制的原点）。
"""

import hashlib
import os
import re
from datetime import datetime
from functools import lru_cache


# ---------------------------------------------------------------- 事件类型
EV_TS = "ts"
EV_TITLE = "title"
EV_PROMPT = "prompt"
EV_ASSISTANT = "assistant"
EV_TOOL = "tool"
EV_WRITE = "write"
EV_TOOL_RESULT = "tool_result"
EV_USAGE = "usage"


def usage_event(u):
    """把各家不一的 usage 结构收敛成一条 usage 事件；拿不到就返回 None。

    字段名在平台之间是分叉的（input_tokens / inputTokens / prompt_tokens），
    这里一次映射完，主程序只认 in / out / cache_read 三个键。

    注意 cache_read 是 in 的子集（WorkBuddy 满足 total = input + output，
    缓存命中量已算在 input 里），下游做成本换算时要按「全价部分 = in - cache_read」
    拆分，不能把两者相加。
    """
    if not isinstance(u, dict) or not u:
        return None

    def pick(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and v >= 0:
                return int(v)
        return 0

    i = pick("input_tokens", "inputTokens", "prompt_tokens")
    o = pick("output_tokens", "outputTokens", "completion_tokens")
    cr = pick("cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens",
              "cachedInputTokens", "cache_read_tokens")
    if not (i or o or cr):
        return None
    return {"kind": EV_USAGE, "in": i, "out": o, "cache_read": cr}

# ---------------------------------------------------------------- 工具分类
# 各家工具名完全不同（Bash / run_command / execute_command / shell），
# 但日志只关心「它对产出意味着什么」。归成 6 类后，跨平台才可累加。
TOOL_WRITE = "write"      # 写盘：Write / Edit / apply_patch
TOOL_READ = "read"        # 读文件
TOOL_SEARCH = "search"    # 检索：grep / glob / 语义搜索 / 网页
TOOL_SHELL = "shell"      # 执行命令：构建、测试、git
TOOL_TASK = "task"        # 任务清单、子代理
TOOL_GENERIC = "generic"  # 知道用途但不属于上面任何一类
TOOL_UNKNOWN = "unknown"  # 完全没见过

# 精确表优先——宁可漏判成 unknown，也不要靠猜把「读」算成「写」。
_TOOL_EXACT = {
    # 写盘
    "write": TOOL_WRITE, "edit": TOOL_WRITE, "multiedit": TOOL_WRITE,
    "notebookedit": TOOL_WRITE, "apply_patch": TOOL_WRITE,
    "create_file": TOOL_WRITE, "write_file": TOOL_WRITE, "edit_file": TOOL_WRITE,
    "str_replace_editor": TOOL_WRITE, "str_replace_based_edit_tool": TOOL_WRITE,
    "insert_edit_into_file": TOOL_WRITE, "search_replace": TOOL_WRITE,
    # 读
    "read": TOOL_READ, "read_file": TOOL_READ, "view_file": TOOL_READ,
    "read_many_files": TOOL_READ, "notebookread": TOOL_READ, "fetch": TOOL_READ,
    # 检索
    "grep": TOOL_SEARCH, "glob": TOOL_SEARCH, "search": TOOL_SEARCH,
    "grep_search": TOOL_SEARCH, "file_search": TOOL_SEARCH,
    "codebase_search": TOOL_SEARCH, "semantic_search": TOOL_SEARCH,
    "list_directory": TOOL_SEARCH, "list_dir": TOOL_SEARCH, "ls": TOOL_SEARCH,
    "websearch": TOOL_SEARCH, "web_search": TOOL_SEARCH, "webfetch": TOOL_SEARCH,
    "web_fetch": TOOL_SEARCH, "fetch_webpage": TOOL_SEARCH,
    # 命令
    "bash": TOOL_SHELL, "shell": TOOL_SHELL, "terminal": TOOL_SHELL,
    "run_command": TOOL_SHELL, "execute_command": TOOL_SHELL, "run_terminal_cmd": TOOL_SHELL,
    "run_shell_command": TOOL_SHELL, "shell_command": TOOL_SHELL,
    "powershell": TOOL_SHELL, "cmd": TOOL_SHELL, "bashoutput": TOOL_SHELL,
    # 任务/编排
    "task": TOOL_TASK, "todo": TOOL_TASK, "todowrite": TOOL_TASK,
    "todo_write": TOOL_TASK, "todo_read": TOOL_TASK, "tasklist": TOOL_TASK,
    "update_plan": TOOL_TASK, "exit_plan_mode": TOOL_TASK,
    "taskcreate": TOOL_TASK, "taskupdate": TOOL_TASK, "taskget": TOOL_TASK,
    "agent": TOOL_TASK, "spawn_agent": TOOL_TASK, "delegate": TOOL_TASK,
}

# 兜底关键词：只在前缀/包含匹配上使用，顺序即优先级。
# 不放 "exec"——它会命中 DeferExecuteTool 这类「工具调度器」，那不是跑命令。
_TOOL_HINTS = (
    ("write", TOOL_WRITE), ("edit", TOOL_WRITE), ("createfile", TOOL_WRITE),
    ("replace", TOOL_WRITE), ("patch", TOOL_WRITE),
    ("grep", TOOL_SEARCH), ("glob", TOOL_SEARCH), ("search", TOOL_SEARCH),
    ("find", TOOL_SEARCH), ("listdir", TOOL_SEARCH),
    ("bash", TOOL_SHELL), ("shell", TOOL_SHELL), ("command", TOOL_SHELL),
    ("terminal", TOOL_SHELL), ("powershell", TOOL_SHELL),
    ("read", TOOL_READ), ("view", TOOL_READ),
    ("todo", TOOL_TASK), ("task", TOOL_TASK), ("plan", TOOL_TASK),
)


def _norm_tool(name: str) -> str:
    """归一工具名：大小写、下划线、连字符、空格一律抹平。

    查表必须和这里用同一套归一，否则 "run_command" 存进表里、查表时
    已被折成 "runcommand"，永远查不中——这类「表里有但命中不了」的
    缺陷不会报错，只会让分类悄悄退化成兜底值。
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# 模块加载时把表键统一折成同一形态，避免两套写法互相打不中
_TOOL_EXACT = {_norm_tool(k): v for k, v in _TOOL_EXACT.items()}


def tool_type(name: str) -> str:
    """把平台工具名收敛成 6 类之一。"""
    key = _norm_tool(name)
    if not key:
        return TOOL_UNKNOWN
    hit = _TOOL_EXACT.get(key)
    if hit:
        return hit
    for frag, kind in _TOOL_HINTS:
        if frag in key:
            return kind
    return TOOL_GENERIC


# 单行 JSONL 的硬上限。工具结果里塞整个文件、或把 base64 图片内联进记录，
# 一条就能到几十 MB。超过上限的行整条丢弃并继续——一条坏记录不该毁掉整个会话。
MAX_LINE = 8 * 1024 * 1024


def safe_lines(path, enc: str = "utf-8"):
    """逐行读取，单行超过 MAX_LINE 时丢弃该行并继续。

    平台写转录是在会话进行中追加的，进程被杀时会留下半行；下面的解析器
    本来就靠 try/except 兜住坏行，这里只额外解决「一行吃掉整个内存」。
    """
    if not path:
        return
    with open(path, encoding=enc, errors="replace") as f:
        for line in f:
            if len(line) > MAX_LINE:
                continue
            yield line


_CMD_KEYS = ("command", "cmd", "commandline", "command_line", "script", "shell_command")

# 真实会话里命令几乎都以「先进目录」开头：cd "D:/x" && dotnet build
# 不剥掉前缀，分类器看到的永远是 cd，所有命令都会退化成 other。
_CD_CHAIN = re.compile(r"""^\s*cd\s+(?:"[^"]*"|'[^']*'|[^\s&;|]+)\s*(?:&&|\|\||;)\s*""")
_ENV_LEAD = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def strip_cd_chain(cmd: str) -> str:
    """剥掉命令前面的 cd / 环境变量前缀，露出真正在跑的那条命令。"""
    c = (cmd or "").strip()
    for _ in range(4):
        new = _CD_CHAIN.sub("", c, count=1)
        if new == c:
            new = _ENV_LEAD.sub("", c, count=1)
        if new == c:
            break
        c = new.strip()
    return c


def extract_cmd(args, limit: int = 200) -> str:
    """从 shell 类工具的入参里取首行命令（已剥掉 cd 前缀）。

    只取首行、只截 200 字：日志要回答的是「今天跑了 build 还是 test」，
    不是把整条命令连同 env 复现一遍。heredoc 会因为首行截断自然退化。
    """
    if not isinstance(args, dict):
        return ""
    raw = ""
    for k in _CMD_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        return ""
    first = strip_cd_chain(raw.splitlines()[0])
    if not first:
        first = raw.splitlines()[0].strip()
    return first[:limit]


# 命令的用途分类。日志里「跑了 3 次构建、5 次测试」是可写进汇报的事实，
# 「执行了 62 条命令」不是。只做前缀/子串匹配，误判成 other 也不会错。
_CMD_CLASS = (
    # 临时脚本：`python -c` / `node -e` / `python - <<EOF` 这类内联代码。
    # 排在构建/测试/依赖前面是必须的——内联代码体里经常出现 pip、git、pytest
    # 字样，放到后面会被整片误判成「装了依赖」或「提交了代码」。
    # 只认 -c/-e 与标准输入两种形态，`python -m pytest` 仍然归测试。
    ("script", re.compile(
        r"(?:python\d?(?:\.\d+)?|py|node|deno|bun|ruby|perl|pwsh|powershell)"
        r"(?:\.exe)?(?![\w-])"
        r"(?:(?:\s+\S+)*?\s+-[ce]\b|\s+-\s*(?:<<|\|))", re.I)),
    ("test", re.compile(r"\b(pytest|jest|vitest|mocha|unittest|tox|nox|"
                        r"go\s+test|cargo\s+test|dotnet\s+test|mvn\s+test|gradle\w*\s+test|"
                        r"npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|ctest)\b"
                        r"|python\d?(\.\d+)?\s+-m\s+(pytest|unittest)", re.I)),
    ("build", re.compile(r"\b(make|cmake|msbuild|dotnet\s+build|cargo\s+build|go\s+build|"
                         r"mvn\s+(package|install)|gradle\w*\s+(build|assemble)|"
                         r"npm\s+run\s+build|yarn\s+build|pnpm\s+build|tsc|webpack|vite\s+build|"
                         r"pyinstaller|nuget\s+restore)\b"
                         r"|python\d?(\.\d+)?\s+-m\s+build", re.I)),
    ("deps", re.compile(r"\b(pip\s+install|npm\s+(i|install|ci)|yarn\s+(add|install)|"
                        r"pnpm\s+(add|i|install)|cargo\s+(add|fetch)|go\s+(get|mod)|"
                        r"dotnet\s+(add|restore)|nuget|apt(-get)?\s+install)\b", re.I)),
    ("git", re.compile(r"\bgit\b", re.I)),
    # 查看类：真实会话里 ls/cat/grep 占了 shell 调用的一半以上。单列出来，
    # 才能说清「今天多数时间在查，不是在构建」——否则它们全沉进 other。
    ("inspect", re.compile(r"\b(ls|dir|cat|head|tail|less|more|find|grep|rg|"
                           r"wc|tree|stat|which|du|df|pwd|env|sed|awk|"
                           r"curl|wget|jq|diff|md5sum|sha1sum)\b", re.I)),
)


def cmd_class(cmd: str) -> str:
    """把命令归成 script / test / build / deps / git / other。"""
    c = (cmd or "").strip()
    if not c:
        return "other"
    for kind, pat in _CMD_CLASS:
        if pat.search(c):
            return kind
    return "other"

# 工具输出中的错误线索（通用启发式，不含平台特征）
ERROR_HINT = re.compile(
    r"(错误|失败|异常|不通过|报错|Error|error:|Exception|CS\d{4}|fail)", re.I)

_SYS_REMINDER = re.compile(r"<system-reminder.*?</system-reminder>", re.S)
_USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_ANY_TAG = re.compile(r"<[^>]{1,60}>")
_SPACES = re.compile(r"[ \t]+")

# 上下文压缩摘要会被平台当作用户消息存下来，它不是提问，混进日志就是噪声
_NOISE_PREFIX = re.compile(
    r"^(Summary of the conversation between"
    r"|This session is being continued"
    r"|Conversation summary"
    r"|以下是(本|这段|上面|此前)?(次)?对话的?(摘要|总结)"
    r"|\[Request interrupted)", re.I)


def iso_to_ms(value):
    """ISO 8601 字符串 → 毫秒整数。各平台时间戳统一成毫秒，才能跨源排序比较。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():          # 部分平台把毫秒存成字符串
        return int(s)
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def clean_prompt(txt: str, inject_pat=None) -> str:
    """剥掉注入的上下文块，只留用户真实提问。

    inject_pat 用于剥离平台特有的成对注入标签（如 WorkBuddy 的身份块），
    不传时只做通用的 system-reminder 与孤立标签清理。
    """
    if not txt:
        return ""
    m = _USER_QUERY.search(txt)
    if m:
        txt = m.group(1)
    else:
        txt = _SYS_REMINDER.sub("\n", txt)
        if inject_pat is not None:
            txt = inject_pat.sub("", txt)
    txt = _ANY_TAG.sub(" ", txt)
    txt = _SPACES.sub(" ", txt).strip()
    if len(txt) < 2 or txt.startswith("Caveat:"):
        return ""
    if _NOISE_PREFIX.match(txt):
        return ""
    return txt


def rel_in_cwd(raw_path: str, cwd: str) -> str:
    """把工具调用里的绝对路径归一为相对工作区的路径。

    只保留落在工作区内的写入；工作区外的写入（改配置文件、写临时目录）一律丢弃。
    返回空串表示「不属于本工作区」。
    """
    norm = (raw_path or "").replace("\\", "/")
    if not norm:
        return ""
    base = (cwd or "").replace("\\", "/").rstrip("/")
    if not base:
        # 没有工作区信息时退化为文件名，与既有行为一致
        return norm.split("/")[-1]
    low, bl = norm.lower(), base.lower()
    if low == bl:
        # 写入路径恰好等于工作区根（实际几乎不会发生），保留原样以维持既有行为
        return norm
    if not low.startswith(bl + "/"):
        return ""
    rel = norm[len(base) + 1:]
    if not rel or rel.startswith(".."):
        return ""
    return rel


# ---------------------------------------------------------------- 仓库身份
_GIT_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_GIT_URL_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def normalize_git_url(url: str) -> str:
    """把各种写法的 remote 归一成 `host/owner/repo`。

    这几种写法必须得到同一个结果，否则同一个仓库换一台机器就是新项目：
        https://github.com/o/r.git   git@github.com:o/r.git   github.com/o/r
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if _GIT_URL_SCHEME.match(u):
        u = _GIT_URL_SCHEME.sub("", u)
    elif "://" not in u and _GIT_URL_SCP.match(u):
        m = _GIT_URL_SCP.match(u)
        u = f"{m.group(1)}/{m.group(2)}"
    # 去掉 user[:token]@ —— 只削第一个 / 之前的那一段，避免误伤路径里的 @
    head, sep, tail = u.partition("/")
    if "@" in head:
        u = head.split("@", 1)[1] + sep + tail
    if u.lower().endswith(".git"):
        u = u[:-4]
    parts = [p for p in u.split("/") if p]
    return "/".join(parts[:3]).lower() if len(parts) >= 3 else u.lower()


def _read_origin_url(git_dir: str) -> str:
    """从 .git/config 里取 origin 的 url。解析失败一律返回空串。"""
    cfg = os.path.join(git_dir, "config")
    if not os.path.isfile(cfg):
        return ""
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r'\[remote\s+"origin"\][^\[]*?url\s*=\s*(\S+)', text, re.I | re.S)
    return m.group(1).strip() if m else ""


def _resolve_git_dir(start: str):
    """从 start 向上找 .git，返回 (git_dir, work_tree)。找不到返回 (None, None)。

    兼容 worktree / submodule：此时 .git 是文件，内容为 `gitdir: <路径>`。
    """
    cur = os.path.abspath(start)
    for _ in range(40):
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            return dot, cur
        if os.path.isfile(dot):
            try:
                with open(dot, encoding="utf-8", errors="replace") as f:
                    head = f.readline().strip()
            except OSError:
                head = ""
            if head.lower().startswith("gitdir:"):
                gd = head.split(":", 1)[1].strip()
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(cur, gd))
                # worktree 的 config 在主仓库的 commondir 里
                common = os.path.join(gd, "commondir")
                if os.path.isfile(common):
                    try:
                        with open(common, encoding="utf-8", errors="replace") as f:
                            rel_common = f.read().strip()
                        gd = os.path.normpath(os.path.join(gd, rel_common))
                    except OSError:
                        pass
                return gd, cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None, None


@lru_cache(maxsize=512)
def git_work_tree(path: str) -> str:
    """向上找最近的 git 工作树根，找不到返回空串。

    只用来把「工作区外的文件」截成仓库内相对路径——日志里
    `src/a.cs` 能直接认，`C:/Users/me/repo/src/a.cs` 会把正文冲成路径堆。
    """
    if not path:
        return ""
    _, wt = _resolve_git_dir(path)
    return wt or ""


def git_identity(path: str):
    """向上找最近的 git 仓库，返回 {'id', 'name', 'remote'}；不在仓库里返回 None。

    为什么需要它：会话的 cwd 和实际干活的目录经常不是同一个（在一个空工作区里
    被指派对另一个仓库动手）。按目录名分组会把同一个仓库拆成几条，按 git 远程
    分组则天然收敛——这也是「跨机器、跨克隆保持同一身份」的唯一稳定键。
    """
    if not path:
        return None
    gd, wt = _resolve_git_dir(path)
    if not gd:
        return None
    remote = normalize_git_url(_read_origin_url(gd))
    name = os.path.basename(wt.rstrip("/\\")) or ""
    if not remote:
        # 本地仓库没有 origin：退化为 work_tree 路径哈希，仅本机可比
        seed = os.path.normcase(os.path.abspath(wt))
    else:
        seed = remote
    h = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]
    return {"id": f"{h[:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}",
            "name": name, "remote": remote}


# ---------------------------------------------------------------- 索引接口
class Index:
    """统一会话索引：主程序只调用这三个方法，不关心索引从哪来。"""

    def ids(self) -> set:
        return set()

    def info(self, sid: str) -> dict:
        return {}

    def transcript(self, sid: str):
        return None


class MultiIndex(Index):
    """把多个平台的索引聚合成一个。

    sid 在各平台之间理论上不会撞（多为 UUID），一旦撞了则加 `<平台>:` 前缀，
    原始 sid 保留在 _origin 里，transcript 仍能正确取到。
    """

    def __init__(self, pairs=None):
        self.meta = {}
        self._origin = {}   # 聚合后的 sid -> (原 index, 原 sid)

        for name, ix in (pairs or []):
            for sid in ix.ids():
                key = sid
                if key in self.meta:
                    key = f"{name}:{sid}"
                info = dict(ix.info(sid) or {})
                info["source"] = name
                self.meta[key] = info
                self._origin[key] = (ix, sid)

    def ids(self):
        return set(self.meta)

    def info(self, sid):
        return self.meta.get(sid, {})

    def transcript(self, sid):
        pair = self._origin.get(sid)
        return pair[0].transcript(pair[1]) if pair else None


# ---------------------------------------------------------------- 适配器基类
class Adapter:
    """平台适配器基类。

    子类必须覆盖 name / display / probe / load_index / iter_events，
    其余方法都有安全默认值：拿不到就返回空，绝不抛错中断整个归档流程。
    """

    name = ""          # 机器标识，写入 manifest 的 source 字段
    display = ""       # 展示名，出现在 README 与日志里

    # ---------- 探测与索引
    def probe(self) -> bool:
        """本机是否存在该平台的数据。返回 False 时整体跳过，不产生任何告警。"""
        return False

    def load_index(self) -> Index:
        """构建本平台的会话索引。"""
        raise NotImplementedError

    def iter_events(self, path, cwd: str = ""):
        """流式产出中立事件。path 为 transcript 路径，cwd 已转为正斜杠小写。"""
        return iter(())

    # ---------- 回跳与补充信息
    def deep_link(self, sid: str) -> str:
        """指向原始对话的深链。无深链能力时返回空串，由 open_hint 兜底。"""
        return ""

    def open_hint(self) -> str:
        """无深链时的文字说明，写入 README 的「打开原对话」小节。"""
        return f"在 {self.display or self.name} 中打开会话 ID："

    def memory_dir(self, workspace):
        """该工作区下的项目记忆目录。无此概念返回 None。"""
        return None

    def workspace_root(self):
        """本平台默认的工作区根目录。无此概念返回 None。"""
        return None
