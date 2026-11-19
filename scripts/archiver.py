#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话项目归档器 (dev-project-archivist)

职责：扫描本机 AI 对话历史，识别其中开发的项目，把「日期 / 用途 / 源代码 /
技术实现 / 会话索引」整理到用户指定目录，并支持增量同步。

架构：
  adapters/<平台>.py  把各平台的会话数据翻译成统一的中立事件流
  archiver.py         归并、判定、抽取、归档、渲染（与平台无关）
  新增平台只需在 adapters/ 下加一个文件，主程序不改。

设计原则：
  1. 确定性解析承担 99% 的工作，大模型只做语言归纳（避免 735MB 转录进上下文）。
  2. 两阶段：scan（采集证据，确定性）→ render（渲染文档，确定性）。
     中间由大模型填 _summary.json，缺省则用模板兜底，保证无模型也能产出完整结果。
  3. 增量：以文件指纹 (rel, size, mtime) + 项目指纹 sha1 判定变化，只处理变化项。
  4. 幂等：README 划出 AUTO 区，用户手写区永不被覆盖；重跑不产生重复内容。
  5. 可回退：删除走 .trash/，归档目录内的文件永不原地销毁。

数据源（由适配器决定，均只读）。内置 WorkBuddy 适配器读取：
  ~/.workbuddy/workbuddy.db                       会话元数据 (sessions 表)
  ~/.workbuddy/projects/<cwd编码>/<sid>.jsonl     对话转录
  <workspace>/.workbuddy/memory/*.md              项目记忆

用法：
  python archiver.py scan    [--dry-run] [--limit N] [--only 关键字] [--force]
  python archiver.py render
  python archiver.py run     # scan + render
  python archiver.py pending # 列出待归纳的证据包（供大模型处理）
  python archiver.py status
"""

import argparse
import hashlib
import html as _html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

# 适配器包与主程序同目录；脚本直跑时 sys.path[0] 已是本目录，
# 但从其他位置 import 时需显式补一次，保证两种方式都能加载。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import REGISTRY, build_index, get_adapter, get_adapters  # noqa: E402
from adapters.base import (EV_ASSISTANT, EV_PROMPT,  # noqa: E402
                           EV_TITLE, EV_TOOL, EV_TOOL_RESULT, EV_TS, EV_USAGE,
                           EV_WRITE, cmd_class, git_identity, git_work_tree,
                           tool_type)

# ---------------------------------------------------------------- 版本
# 与 pyproject.toml 的 version、CHANGELOG.md 最新条目标题必须一致。
# 三处同步是发版动作的一部分，`test_release.py` 会核对——版本号飘了比没版本号更糟：
# 用户报 bug 说"0.3.0 有问题"，代码里却是别的数，排查从第一句就开始错。
#
# 版本号口径：**等于计划中已完成档位的最高一个**，不提前（虚报）、不落后（漏报）。
# 阶段名属计划文档，版本号属产物。
__version__ = "0.4.0"

# ---------------------------------------------------------------- 控制台编码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 路径常量
HOME = Path.home()

# 各平台的会话数据源路径已随适配器迁至 adapters/<平台>.py。
# 此处只保留判定「AI 从零创建的工作区」所需的根目录，供 Project.compute 使用。
WORKSPACES_ROOT = HOME / "WorkBuddy"

# 默认输出目录：优先读环境变量 ARCHIVER_TARGET，未设置则落在用户主目录下。
# 这样换机或他人使用时无需任何配置即可运行，不假设存在特定盘符。
DEFAULT_TARGET = Path(os.environ.get("ARCHIVER_TARGET")
                      or (HOME / "Workbuddy项目归档"))

STATE_FILE = "state.json"
INDEX_MD = "INDEX.md"
INDEX_JSON = "index.json"
CHANGELOG = "CHANGELOG.md"
EVIDENCE_DIR = "_evidence"
SUMMARY_FILE = "_summary.json"
MANIFEST_FILE = "manifest.json"
META_DIR = "_meta"

# ---------------------------------------------------------------- 可调参数
DEFAULTS = {
    "max_file_mb": 5,          # 单文件超过此体积只登记不复制
    "max_resource_kb": 256,    # 运行时资源保留阈值，超出仅登记到 external_deps
    "small_project_mb": 20,    # （保留兼容）整目录复制的体积界线，现已由分级策略取代
    "max_copy_mb": 40,         # 单项目源码复制上限（纯源码，40MB 已很宽松）
    "max_docs_mb": 20,         # 单项目 _docs/ 设计文档上限
    "max_evidence_chars": 14000,
    "max_memory_chars": 6000,
    "max_recovered_kb": 1024,  # 从转录复活已删文件的总量上限
    "max_snippets": 14,
    "min_code_files": 5,       # 判定为代码项目的源码文件数下限
    "git_enabled": True,       # 每个项目目录独立 git init
}

# .git 体积超过此值时 status 提示手动 gc。不做自动 gc：
# 它会在每日定时任务里拖长执行时间，得不偿失。
GIT_WARN_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------- 扩展名表
CODE_EXT = {
    ".cs", ".xaml", ".csproj", ".sln", ".props", ".targets",
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".java", ".kt", ".kts", ".go", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".php", ".rb", ".swift", ".m", ".mm", ".lua", ".dart", ".scala", ".sh", ".ps1", ".bat", ".cmd",
    ".sql", ".gd", ".qml",
}

TEXT_EXT = {
    ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
    ".md", ".txt", ".rst", ".log", ".env.example", ".gitignore", ".editorconfig",
    ".dockerfile", ".cmake", ".gradle", ".pom", ".plist", ".manifest", ".resx", ".config",
}

DOC_EXT = {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls", ".md", ".txt",
           ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp", ".zip", ".rar", ".7z"}

MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg", ".mp4",
             ".mp3", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".onnx", ".pth", ".pt",
             ".dll", ".exe", ".so", ".dylib", ".lib", ".pdb", ".nupkg", ".zip", ".rar", ".7z"}

# ---- 归档分级（决定「留源码 / 另存文档 / 仅登记」）-----------------------------
# 设计文档类：不进 source/，另存 _docs/
OFFICE_EXT = {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf", ".odt"}

# 运行时必需资源：体积小则随源码保留，超过阈值仅登记
RESOURCE_EXT = {".ico", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
                ".ttf", ".otf", ".woff", ".woff2", ".xml", ".json", ".csv", ".dat",
                ".resx", ".qrc", ".ui", ".glade", ".wav", ".mp3", ".bin"}

# 图片/字体：只有位于资源目录（assets/res/icons 等）才算项目资源，
# 其余多为测试样本、截图、分析图表，一律不进源码副本
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
             ".tif", ".tiff", ".psd", ".ai"}
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2", ".eot"}

# 资源目录特征：命中则视为项目自带资源
ASSET_DIR_PAT = re.compile(
    r"(^|/)(assets?|res|resources|images?|img|icons?|static|wwwroot|fonts?|"
    r"shaders?|themes?|media|public)/", re.I)
ICON_NAME_PAT = re.compile(r"(icon|logo|splash|avatar|placeholder|favicon|banner)", re.I)

# 数据文件：体积大则仅登记
DATA_EXT = {".json", ".xml", ".csv", ".tsv", ".npz", ".npy", ".pkl", ".dat",
            ".mat", ".sqlite", ".db", ".parquet"}

# 纯噪声：任何情况下都不复制（构建产物 / 日志 / 压缩包 / 二进制依赖）
NOISE_EXT = {".log", ".pdb", ".dll", ".exe", ".so", ".dylib", ".lib", ".a", ".o",
             ".obj", ".nupkg", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
             ".pyc", ".pyo", ".class", ".jar", ".war", ".onnx", ".pth", ".pt",
             ".tflite", ".caffemodel", ".pb", ".h5", ".ckpt", ".safetensors",
             ".msi", ".cab", ".iso", ".dmg", ".pkg", ".tmp", ".bak", ".swp",
             ".cache", ".ilk", ".exp", ".map", ".tsbuildinfo", ".pyc"}

# 生成物命名特征（报告 / 样本输出 / 临时文件）
NOISE_NAME_PAT = re.compile(
    r"(report|sample|output|result|dump|_diff|_log|^tmp|^temp|_tmp_|_bak|\.old$|"
    r"~\$|_copy\b|副本)", re.I
)

SENSITIVE_NAMES = {".env", "id_rsa", "id_dsa", ".npmrc", ".pypirc", "credentials"}
SENSITIVE_EXT = {".key", ".pem", ".pfx", ".p12", ".keystore"}

IGNORE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "bower_components",
    "dist", "build", "out", "target", "bin", "obj", "__pycache__",
    ".venv", "venv", "env", "packages", ".vs", ".idea", ".vscode",
    ".next", ".nuxt", ".svelte-kit", ".gradle", ".dart_tool", "Pods",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "coverage",
    ".git-crypt", "NDependOut", ".workbuddy",
    # 各 AI 工具在项目内留下的本地配置/缓存，不属于源码
    ".claude", ".cursor", ".codex", ".aider", ".continue", ".gemini",
    # 以下为临时/测试产物目录：不属于可移交源码
    "bin_tmp", "testresults", "htmlcov", "logs", "packages-cache",
    "blazor.boot.json.d", ".angular",
}

# 以这些前缀开头的目录一律视为临时目录（_tmp_build / _optdb / _probe ...）
IGNORE_DIR_PREFIX = ("_tmp", "_optdb", "_probe", "_scratch", "_temp", "_backup",
                     "_bak", "_old", "_trash", "_debug", "_diag", "_cache")

# 工程入口文件：判定「源码是否完整」的锚点（扩展名）
ENGINE_EXT = {".sln", ".csproj", ".vcxproj", ".vbproj", ".fsproj", ".gradle",
              ".pro", ".pri", ".spec", ".xcodeproj", ".pbxproj", ".cabal"}

# 工程入口文件：按文件名匹配
ENGINE_NAMES = {"package.json", "requirements.txt", "pyproject.toml", "setup.py",
                "pipfile", "poetry.lock", "cmakelists.txt", "makefile", "cargo.toml",
                "go.mod", "pom.xml", "build.gradle", "settings.gradle", "pubspec.yaml",
                "composer.json", "gemfile", "mix.exs", "tsconfig.json"}

# 备份/历史副本：一律剔除（这类文件往往成百上千，是目录膨胀主因）
BACKUP_SCRIPT_PAT = re.compile(
    r"(_bak|_backup|备份|_archive|_frozen|_old\d*|_prev|_copy|副本|改动前|_before|"
    r"^bak\d|^backup\d|~$)",
    re.I)

# 一次性脚本特征：对话过程中产生的诊断/修复脚本，不属于项目源码。
# 注意：不把 ^fix_ 纳入——某些项目的核心算法脚本就叫 fix_xxx.py，需保留。
TEMP_SCRIPT_PAT = re.compile(
    r"(^repro|^probe|^diag|^inspect_|^dump|^scratch|^oneoff|^tmp_|^temp_|^try_|"
    r"^check_|^chk_|^verify_|^patch_|^gen_|^_|^analyze_|^devcheck|^nrtest|^cmp_|"
    r"^cleanup|^clean_|^port_|^merge_to|^litscan|^idxplan|^fsprobe|^pecheck|"
    r"^db_stats|^know_verify|^stat_|_dbg|_stat$|_stat_|"
    r"_(test|tests|stats|dump|tmp|temp|bak)\.(py|ps1|sh|bat|js)$|"
    r"_v\d{2,3}_|_v\d{2,3}\.(py|ps1|sh|bat|js)$|_\d{6,8}\.(py|ps1|sh|bat|js)$)", re.I
)

# 构建/启动类脚本必须保留（白名单优先于上面的剔除规则）
KEEP_SCRIPT_PAT = re.compile(
    r"^(build|compile|make|run|start|launch|deploy|publish|release|install|setup|"
    r"init|bootstrap|serve|dev|watch|test|clean|package|pack|bundle|env)[-_.]?",
    re.I
)

BARE_NAME_ALLOW = {"readme", "license", "makefile", "dockerfile", "procfile",
                   "gemfile", "rakefile", "cmakelists.txt", ".gitignore", ".editorconfig"}

LANG_BY_EXT = {
    ".cs": "C#", ".xaml": "XAML", ".csproj": "C#", ".sln": "C#",
    ".py": "Python", ".pyw": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".vue": "Vue", ".svelte": "Svelte",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++ 头文件", ".hpp": "C++",
    ".php": "PHP", ".rb": "Ruby", ".swift": "Swift", ".m": "Objective-C",
    ".lua": "Lua", ".dart": "Dart", ".scala": "Scala",
    ".sh": "Shell", ".ps1": "PowerShell", ".bat": "Batch", ".cmd": "Batch",
    ".sql": "SQL", ".gd": "GDScript", ".qml": "QML",
}

LANG_ORDER = ["C#", "Python", "JavaScript", "TypeScript", "HTML", "CSS", "Vue",
              "Java", "Go", "Rust", "C++", "C", "SQL", "PowerShell", "Shell", "Batch"]

DECISION_KW = re.compile(
    r"(方案|决定|结论|选型|权衡|根因|修复|踩坑|改为|改用|采用|定案|不建议|建议先|"
    r"原因在于|导致|回退|降级|替代|最终选择|取舍)"
)
SECRET_TOKEN_PAT = re.compile(r"sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
SECRET_KV_PAT = re.compile(
    r"(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*[\"']?[^\s\"',;}]{6,}",
    re.I,
)


# ================================================================ 基础工具
def now_ts() -> float:
    return time.time()


def iso(ts) -> str:
    """时间戳(秒/毫秒) 或 已格式化字符串 → 本地 ISO 字符串"""
    if not ts:
        return ""
    if isinstance(ts, str):
        s = ts.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:16].replace("T", " ")
        try:
            ts = float(s)
        except Exception:
            return ""
    ts = float(ts)
    if ts > 1e11:  # 毫秒
        ts /= 1000.0
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def day(ts) -> str:
    s = iso(ts)
    return s[:10] if s else ""


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def dir_bytes(p: Path) -> int:
    """目录实际占用的字节数（用于 .git 体积体检）"""
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for fn in files:
                try:
                    total += (Path(root) / fn).stat().st_size
                except Exception:
                    pass
    except Exception:
        return total
    return total


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def sha1_file(p: Path, limit_mb: int = 8) -> str:
    h = hashlib.sha1()
    try:
        if p.stat().st_size > limit_mb * 1024 * 1024:
            return f"size:{p.stat().st_size}"
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def atomic_write(path: Path, text: str):
    """原子写入：先写 .tmp 再 os.replace，避免断电留半文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(p: Path, limit: int = None) -> str:
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except Exception:
        return ""


def md_cell(s, limit: int = 0) -> str:
    """清洗 Markdown 表格单元格：去换行、竖线替换、压缩空白"""
    s = (s or "").replace("\r", " ").replace("\n", " ").replace("|", "／")
    s = re.sub(r"\s{2,}", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def norm_key_manage(key: str) -> str:
    """人工配置键规范化：与管理数据层 norm_key 保持一致"""
    return (key or "").replace("\\", "/").rstrip("/").lower()


def load_overrides(target: Path) -> dict:
    """读取人工校正表，返回 {规范化键: 配置}。键支持工作区路径或归档目录名。

    文件结构示例见 overrides.json（首次运行自动生成）。
    """
    p = target / "overrides.json"
    if not p.exists():
        atomic_write(p, json.dumps({
            "_说明": "人工校正表。键可填工作区路径或归档目录名；"
                     "title/purpose 覆盖自动归纳结果，skip=true 表示不归档该工作区，"
                     "type 可强制指定『代码项目/文档任务/混合产物』。改完重跑 render 生效。",
            "_示例": {
                "<工作区绝对路径，或归档目录名>": {
                    "title": "自定义项目名",
                    "purpose": "这个项目实际做的是……（覆盖自动归纳）",
                    "type": "代码项目"
                }
            }
        }, ensure_ascii=False, indent=2))
        return {}
    try:
        raw = json.loads(read_text(p) or "{}")
    except Exception:
        return {}
    out = {}
    for k, v in (raw or {}).items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        out[k.replace("\\", "/").rstrip("/").lower()] = v
    return out


def pick_override(ov: dict, key: str, dirname: str) -> dict:
    """按工作区键或目录名查找校正项"""
    if not ov:
        return {}
    hit = ov.get((key or "").replace("\\", "/").rstrip("/").lower())
    if hit:
        return hit
    return ov.get((dirname or "").lower(), {})


def short_title(t: str, maxlen: int = 22) -> str:
    """把一句口语化需求压成短标题：切在第一个标点，超长截断"""
    # 开头的 @ / 井号 / 斜杠是编辑器标记或注释符，不是内容
    t = re.sub(r"^[@\s#/\\]+", "", (t or "").strip())
    # 冒号只在「不是协议分隔符」时才切：否则 "https://x" 会被切成 "https"
    t = re.split(r"[，。；：,;！？!?\n]|:(?!//)", t)[0].strip()
    return t[:maxlen] if len(t) > maxlen else t


JUNK_TITLE_PAT = re.compile(
    r"^(e|d|c|a|b|summary|image#?\d+|screenshot\d*|clipboard[_-]?\d*|"
    r"untitled|new chat|新对话|\d+)$", re.I)

# 纯链接 / 纯路径 / 纯主机名：这是「用户贴了个地址」，不是项目名
URL_LIKE_PAT = re.compile(r"(?i)^(https?|ftp|file|www\.|git@|ssh://)")
PATH_LIKE_PAT = re.compile(r"^(?:[A-Za-z]:[\\/]|[/\\]|~[\\/]|\.{1,2}[\\/])")
HOST_ONLY_PAT = re.compile(
    r"(?i)^(?:localhost|github|gitee|gitlab|google|baidu|bing)(?:\.com)?$")

# 句子感：出现这些词说明是「一句话」，不是「一个名字」
SENTENCE_HINT = re.compile(
    r"(为什么|怎么办|如何|怎么|是否|能不能|可不可以|请问|帮我|麻烦|我想|"
    r"我们|需要|要求|下面|现在|一下)")


def is_junk_title(t: str) -> bool:
    """识别明显不适合当项目名的标题。

    三类：占位名（单字母、截图名）、纯地址（URL/路径/主机名）、句子（口语化请求）。
    最后一条最要紧——实测项目名里出现过 `https`、`密码为什么为空 LoginPanel`，
    根源是把「用户随口说的话」当成了「项目叫什么」。
    """
    t = (t or "").strip()
    if len(t) < 4:
        return True
    if JUNK_TITLE_PAT.match(t):
        return True
    if URL_LIKE_PAT.match(t) or PATH_LIKE_PAT.match(t) or HOST_ONLY_PAT.match(t):
        return True
    # 句子：长度够长且带疑问/请求语气一律不认；短句仍保留，信息量还在
    if len(t) >= 14 and SENTENCE_HINT.search(t):
        return True
    return False


def url_host_name(t: str) -> str:
    """纯链接标题退化为主机名：https://www.example.org/chapterq?... → example.org

    链接当标题时，主机名比「Python 项目」有信息量得多——三个同类项目
    退回语言兜底，名字就完全一样了，用户根本分不清。
    """
    m = re.search(r"(?i)^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", t or "")
    return m.group(1) if m else ""


def pick_title(titles: list) -> str:
    """从若干候选标题中挑最像『项目名』的一个（短、不含请求语气）。

    三级兜底：像名字的标题 → 链接的主机名 → 剩下的句子（截断）。
    最后一级是被迫的：一个能区分彼此的长句，好过三个都叫「Python 项目」。
    """
    cands = [t for t in titles if t and t.strip() and not is_junk_title(t)]
    if cands:
        def score(t):
            s = 0.0
            if len(t) <= 24:
                s += 2
            if not SENTENCE_HINT.search(t):
                s += 2
            if re.search(r"(系统|工具|平台|应用|插件|归档|助手|App|app)", t):
                s += 1
            return s - len(t) / 100.0

        return short_title(max(cands[:6], key=score), 28)

    for t in titles:
        if t and URL_LIKE_PAT.match(t.strip()):
            h = url_host_name(t)
            if h:
                return h

    weak = [t for t in titles
            if t and len(t.strip()) >= 4 and not JUNK_TITLE_PAT.match(t.strip())]
    return short_title(weak[0], 28) if weak else ""


def safe_name(s: str, maxlen: int = 40) -> str:
    """生成文件名安全的项目名（保留中文，剔除 Windows 非法字符）"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s[:maxlen].strip()
    return s or "未命名项目"


def desensitize(s: str) -> str:
    """脱敏：抹掉 API key / token / 密码"""
    if not s:
        return s
    s = SECRET_TOKEN_PAT.sub("<REDACTED>", s)
    s = SECRET_KV_PAT.sub(
        lambda m: re.split(r"[:=]", m.group(0), maxsplit=1)[0] + "=<REDACTED>", s)
    return s


# 区外写入经常顺手改到配置与凭据：.ssh/config、.env、id_rsa 之类。
# 归档侧本来就按 SENSITIVE_NAMES 剔除，但日志是「给人看」的出口，必须同样拦住——
# 路径本身就是信息（哪台机器、哪个云账号），何况它还会被贴进群里。
SENSITIVE_PATH_HINT = re.compile(
    r"(^|/)(\.ssh|\.aws|\.kube|\.gnupg|\.docker|credentials|secrets?)(/|$)"
    r"|(^|/)\.env(\.|$)|id_rsa|id_ed25519|\.pem$|\.pfx$|\.p12$", re.I)


def safe_path_label(p: str) -> str:
    """敏感路径在日志里只留占位符，不暴露具体位置。"""
    return "（敏感路径已隐藏）" if SENSITIVE_PATH_HINT.search(str(p).replace("\\", "/")) else str(p)


def is_sensitive(p: Path) -> bool:
    n = p.name.lower()
    return n in SENSITIVE_NAMES or p.suffix.lower() in SENSITIVE_EXT


def should_skip_dir(name: str) -> bool:
    n = name.lower()
    if n in IGNORE_DIRS:
        return True
    return any(n.startswith(pfx) for pfx in IGNORE_DIR_PREFIX)


def is_engine_file(name: str, ext: str) -> bool:
    """是否为工程入口文件（决定源码可构建性的锚点）"""
    return ext in ENGINE_EXT or name.lower() in ENGINE_NAMES


# ---------------------------------------------------------------- 项目身份
# 平台自动生成的工作区目录：时间戳型（2026-11-18-21-34-40 / 20260328171213）。
# 这类名字对用户毫无辨识度，绝不能当选项目名。
PLATFORM_DIR_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d{2}-\d{2}-\d{2})?$|^\d{8,14}$")
# 目录名本身是通用词时，同样没有辨识度
GENERIC_DIRS = {"src", "source", "app", "main", "test", "tests", "build", "dist",
                "repo", "project", "workspace", "code", "temp", "tmp", "new folder",
                "untitled", "downloads", "desktop", "documents", "my documents",
                "www", "wwwroot", "public", "static"}
# 清单文件里的占位名，不能当项目名
GENERIC_PKG_NAMES = {"app", "my-app", "myapp", "project", "example", "demo", "test",
                     "main", "root", "package", "workspace", "untitled", "com", "org"}


def _clean_identity(raw: str) -> str:
    """把仓库地址/包名清成可用名：去协议、去 userinfo、去 .git、去 org 前缀。"""
    s = (raw or "").strip().strip("\"'")
    s = re.sub(r"\.git/*$", "", s, flags=re.I)
    if "://" in s:
        s = s.split("://", 1)[1]               # https://host/org/repo → host/org/repo
        s = re.sub(r"^[^/@]*@", "", s)         # 万一还有 userinfo
    elif ":" in s and "/" in s:
        s = s.split(":", 1)[1]                 # git@host:org/repo → org/repo
    s = s.rstrip("/").rsplit("/", 1)[-1]
    return s.strip()


def _accept_identity(s: str) -> bool:
    """身份名是否可用：长度、通用词、占位名、句子感都要过。

    注意这里**不能直接复用 is_junk_title**：它把「长度 < 4」也判为垃圾，
    而 `CDN`、`QMS` 这类三字母项目名是真实存在的（实测就把 CDN 误杀成
    「C# 项目」）。身份名的下限是 2 个字符，只拦纯占位名。
    """
    t = (s or "").strip()
    if len(t) < 2 or len(t) > 40:
        return False
    low = t.lower()
    if low in GENERIC_PKG_NAMES or low in GENERIC_DIRS:
        return False
    if JUNK_TITLE_PAT.match(t):
        return False
    if URL_LIKE_PAT.match(t) or PATH_LIKE_PAT.match(t) or HOST_ONLY_PAT.match(t):
        return False
    if len(t) >= 14 and SENTENCE_HINT.search(t):
        return False
    return True


def git_remote_name(ws: Path) -> str:
    """从 origin 远端取仓库名。

    带凭据的 URL（https://user:token@host/repo.git）必须先剜掉 userinfo——
    这个名字会进目录名与 README，token 不能跟着走。
    """
    import subprocess
    if not (ws / ".git").exists():
        return ""
    try:
        r = subprocess.run(["git", "-C", str(ws), "config", "--get", "remote.origin.url"],
                           capture_output=True, text=True, timeout=8,
                           encoding="utf-8", errors="replace")
    except Exception:
        return ""
    url = (r.stdout or "").strip()
    if not url:
        return ""
    if "://" in url:
        url = re.sub(r"://[^/@]*@", "://", url)
    return _clean_identity(url)


def _shallowest(rels: list, suffix: str) -> list:
    """取指定后缀的文件，只保留目录最浅的一层（避免子工程抢名）"""
    hit = [r for r in rels if r.lower().endswith(suffix)]
    if not hit:
        return []
    depths = [r.replace("\\", "/").count("/") for r in hit]
    d0 = min(depths)
    return [r for r, d in zip(hit, depths) if d == d0]


def manifest_name(ws: Path, rels: list) -> str:
    """从工程清单取项目名。只认「最浅一层」的清单，且同层有歧义时放弃。"""
    low = {r.replace("\\", "/").lower(): r for r in rels}

    if "package.json" in low:
        try:
            data = json.loads(read_text(ws / low["package.json"], 200000) or "{}")
            n = data.get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
        except Exception:
            pass

    for fname in ("pyproject.toml", "cargo.toml"):
        if fname in low:
            m = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']',
                          read_text(ws / low[fname], 200000))
            if m:
                return m.group(1).strip()

    if "go.mod" in low:
        m = re.search(r"(?m)^module\s+(\S+)",
                      read_text(ws / low["go.mod"], 200000))
        if m:
            return m.group(1).strip().rsplit("/", 1)[-1]

    # .NET：优先 AssemblyName，其次文件名；同层多个视为歧义
    for suf in (".csproj", ".sln"):
        cands = _shallowest(rels, suf)
        if len(cands) != 1:
            continue
        rel = cands[0]
        if suf == ".csproj":
            m = re.search(r"<AssemblyName>\s*([^<]+?)\s*</AssemblyName>",
                          read_text(ws / rel, 200000))
            if m and m.group(1).strip():
                return m.group(1).strip()
        return Path(rel).stem
    return ""


def identity_name(ws: Path, rels: list) -> tuple:
    """按「身份强度」取项目名，返回 (名字, 依据说明)。

    强度：git 远端 > 工程清单 > 工作区目录名。三者都拿不到才交给会话标题——
    会话标题是「用户随口说的话」，不是「项目叫什么」，只能当最后的兜底。
    """
    n = git_remote_name(ws)
    if _accept_identity(n):
        return n, "git remote origin"
    n = manifest_name(ws, rels)
    if _accept_identity(n):
        return n, "工程清单"
    base = ws.name
    if base and not PLATFORM_DIR_PAT.match(base) and _accept_identity(base):
        return base, "工作区目录名"
    return "", ""


def is_temp_script(rel: str, name: str) -> bool:
    """判断是否为「对话过程中产生的一次性脚本」。

    只针对脚本类扩展名；构建/启动类脚本白名单优先保留；
    位于 tests/ 等正规测试目录下的文件不算临时脚本。
    """
    ext = Path(name).suffix.lower()
    if ext not in {".py", ".pyw", ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs", ".lua"}:
        return False
    # 备份/历史副本优先剔除（即使名字看起来像构建脚本）
    if BACKUP_SCRIPT_PAT.search(name):
        return True
    if KEEP_SCRIPT_PAT.match(name):
        return False
    low = rel.lower()
    # 正规测试/示例/工具目录里的脚本视为工程组成部分
    for keep_dir in ("test/", "tests/", "testcases/", "examples/", "samples/",
                     "tools/", "scripts/", "build/", "ci/", ".github/"):
        if low.startswith(keep_dir) or ("/" + keep_dir) in low:
            # 但明显的一次性命名仍剔除
            return bool(TEMP_SCRIPT_PAT.search(name))
    return bool(TEMP_SCRIPT_PAT.search(name))


def classify_for_archive(rel: str, size: int, name: str, ext: str,
                         max_resource_kb: int) -> str:
    """文件归档分级：source / docs / resource / bigresource / noise / script

    - source      纯源码与工程配置，进 source/
    - docs        设计文档等，进 _docs/
    - resource    运行时资源，小体积进 source/
    - bigresource 较大资源/数据，仅登记到 manifest，不复制
    - noise       构建产物、日志、截图、样本、二进制 → 剔除
    - script      一次性脚本 → 剔除并登记
    """
    low_rel = "/" + rel.lower()
    is_asset_dir = bool(ASSET_DIR_PAT.search(low_rel))
    fit = size <= max_resource_kb * 1024

    if is_temp_script(rel, name):
        return "script"
    if ext in NOISE_EXT:
        return "noise"
    if ext in OFFICE_EXT:
        return "docs"

    # 图片与字体：仅资源目录内的、体积合适的才算项目资源
    if ext in IMAGE_EXT or ext in FONT_EXT:
        if is_asset_dir or ICON_NAME_PAT.search(name):
            return "resource" if fit else "bigresource"
        return "noise"

    # 数据文件：体积合适则随源码，超出仅登记
    if ext in DATA_EXT:
        return "resource" if fit else "bigresource"

    if is_engine_file(name, ext):
        return "source"
    if ext in CODE_EXT:
        # 生成型 HTML/文本（报告、样本输出）不算源码
        if ext in (".html", ".htm") and NOISE_NAME_PAT.search(name):
            return "noise"
        return "source"
    if ext in TEXT_EXT:
        if NOISE_NAME_PAT.search(name) and ext not in (".json", ".xml", ".yml", ".yaml"):
            return "noise"
        return "source"
    if ext in RESOURCE_EXT:
        return "resource" if fit else "bigresource"
    if name.lower() in BARE_NAME_ALLOW:
        return "source"
    if not ext:
        return "source" if name.lower() in BARE_NAME_ALLOW else "noise"
    return "resource" if fit else "bigresource"


def log(msg: str, verbose: bool = True):
    if verbose:
        print(msg, flush=True)


def adapter_of(source: str = ""):
    """按会话来源取适配器实例；来源未知时回退到默认适配器。"""
    return REGISTRY.get(source or "") or get_adapter()


def deep_link_of(sid: str, source: str = "") -> str:
    """生成指向原始对话的深链；平台不支持深链时返回空串，由调用方降级为文字提示。"""
    if not sid:
        return ""
    try:
        return adapter_of(source).deep_link(sid)
    except Exception:
        return ""


def open_hint_of(source: str = "") -> str:
    """无深链平台的回跳说明文案。"""
    try:
        return adapter_of(source).open_hint()
    except Exception:
        return "在对应的 AI 工具中打开该会话"


# ================================================================ 转录解析
class SessionParser:
    """流式解析单个会话转录，抽取需求、工具画像、写盘动作、决策片段"""

    def __init__(self, path: Path, cwd: str):
        self.path = path
        self.cwd = (cwd or "").replace("\\", "/").lower()
        self.user_prompts = []
        self.titles = []
        self.tool_counts = {}
        self.written = {}        # rel -> content（最后一次写入）
        self.touched = {}        # rel -> op（含只有路径的编辑动作）
        self.decisions = []
        self.errors = []
        self.assistant_chars = 0
        self.first_ts = None
        self.last_ts = None
        self.turns = 0

    # ---------- 主入口
    def parse(self, adapter=None, want_written: bool = False):
        """按指定适配器的事件流解析；不传 adapter 时使用默认平台。"""
        adapter = adapter or get_adapter()
        return self.consume(adapter.iter_events(self.path, self.cwd), want_written)

    def consume(self, events, want_written: bool = False):
        """消费中立事件流，完成全部抽取。

        平台差异（JSON 结构、字段名、注入块）已在适配器层抹平，
        这里只处理语义，不认识任何平台的原始格式。
        """
        for ev in events:
            kind = ev.get("kind")
            if kind == EV_TS:
                ts = ev.get("value")
                if ts:
                    self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
                    self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
            elif kind == EV_TITLE:
                ti = (ev.get("text") or "").strip()
                if ti and ti not in self.titles:
                    self.titles.append(ti)
            elif kind == EV_PROMPT:
                txt = (ev.get("text") or "").strip()
                if txt:
                    self.user_prompts.append(txt)
                    self.turns += 1
            elif kind == EV_ASSISTANT:
                txt = ev.get("text") or ""
                self.assistant_chars += len(txt)
                self._collect_decisions(txt)
            elif kind == EV_TOOL:
                name = ev.get("name") or ""
                self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
            elif kind == EV_WRITE:
                rel, content = ev.get("rel"), ev.get("content")
                if rel and isinstance(content, str) and content:
                    self.written[rel] = content
                elif rel:
                    # 只有路径的编辑动作（op=edit）：归档不用，但按天聚合要用
                    self.touched[rel] = ev.get("op") or "edit"
            elif kind == EV_TOOL_RESULT:
                txt = (ev.get("text") or "").strip()
                if txt:
                    self.errors.append(txt[:220])
        return self

    def _collect_decisions(self, txt: str):
        if not txt or len(self.decisions) >= DEFAULTS["max_snippets"] * 3:
            return
        for para in re.split(r"\n{2,}", txt):
            p = para.strip().replace("\n", " ")
            if 20 <= len(p) <= 400 and DECISION_KW.search(p):
                if not any(p[:40] == d[:40] for d in self.decisions):
                    self.decisions.append(p)

    # ---------- 汇总
    def summary(self) -> dict:
        return {
            "turns": self.turns,
            "prompts": self.user_prompts,
            "titles": self.titles,
            "tool_counts": self.tool_counts,
            "decisions": self.decisions[:DEFAULTS["max_snippets"]],
            "errors": self.errors[:8],
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "written": self.written,
            "assistant_chars": self.assistant_chars,
        }


# ================================================================ 文件扫描
def scan_tree(root: Path) -> list:
    """遍历工作区，返回 [(rel_path, size, mtime, ext)]，跳过噪声目录"""
    out = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except Exception:
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except Exception:
                continue
            out.append((rel, st.st_size, int(st.st_mtime), p.suffix.lower()))
    out.sort(key=lambda x: x[0])
    return out


def classify_files(files: list) -> dict:
    """按扩展名统计，得出语言分布与技术特征"""
    code = doc = media = other = 0
    lang_cnt = {}
    total = 0
    for rel, size, mt, ext in files:
        total += size
        if ext in CODE_EXT:
            code += 1
            lang = LANG_BY_EXT.get(ext)
            if lang:
                lang_cnt[lang] = lang_cnt.get(lang, 0) + 1
        elif ext in DOC_EXT:
            doc += 1
        elif ext in MEDIA_EXT:
            media += 1
        else:
            other += 1
    langs = sorted(lang_cnt.items(), key=lambda x: (-x[1], x[0]))
    return {
        "code": code, "doc": doc, "media": media, "other": other,
        "total_files": len(files), "total_bytes": total,
        "langs": langs,
        "language": " / ".join(l for l, _ in langs[:2]) if langs else "—",
    }


def detect_tech(root: Path, files: list) -> list:
    """从工程标记文件推断技术栈（有价值的『技术实现』素材）"""
    tech = []
    rels = {f[0].lower() for f in files}

    def has(suffix):
        return any(r.endswith(suffix) for r in rels)

    if has(".sln") or has(".csproj"):
        tech.append(".NET")
    if has("package.json"):
        tech.append("Node.js")
        pj = root / "package.json"
        if pj.exists():
            try:
                data = json.loads(read_text(pj, 200000) or "{}")
                deps = list((data.get("dependencies") or {}).keys())[:6] + \
                       list((data.get("devDependencies") or {}).keys())[:4]
                if deps:
                    tech.append("依赖: " + ", ".join(deps[:8]))
            except Exception:
                pass
    if has("requirements.txt") or has("pyproject.toml") or has("setup.py"):
        tech.append("Python")
        req = root / "requirements.txt"
        if req.exists():
            lines = [l.strip() for l in read_text(req, 50000).splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            if lines:
                tech.append("依赖: " + ", ".join(lines[:8]))
    if has("index.html") and not (has(".sln") or has(".csproj")):
        tech.append("静态 Web 页面")
    if has("dockerfile"):
        tech.append("Docker")
    if has("google-services.json") or has("androidmanifest.xml"):
        tech.append("Android")
    if any(r.endswith(".unity") for r in rels):
        tech.append("Unity")
    # csproj 里的 NuGet 包引用
    for rel, *_ in files:
        if rel.lower().endswith(".csproj"):
            txt = read_text(root / rel, 200000)
            pkgs = re.findall(r'PackageReference\s+Include="([^"]+)"', txt)
            if pkgs:
                tech.append("NuGet: " + ", ".join(sorted(set(pkgs))[:8]))
            tf = re.findall(r"<TargetFramework>([^<]+)<", txt)
            if tf:
                tech.append("目标框架: " + tf[0])
            break
    return tech


# ================================================================ 项目聚合
class Project:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.sessions = []      # [ {sid,title,created,updated,automation,parse} ]
        self.kind = "ai_workspace"
        self.files = []
        self.cls = {}
        self.tech = []
        self.memory = ""
        self.first_ts = None
        self.last_ts = None
        self.name = ""
        self.name_src = ""      # 名字取自哪个证据（git remote / 工程清单 / 目录名 / 会话标题）
        self.entries = []       # 工程入口文件，供 --explain 展示判定依据
        self.key = ""

    def compute(self, min_code: int):
        self.key = self.workspace.replace("\\", "/").rstrip("/").lower()
        ws = Path(self.workspace)
        root_ai = WORKSPACES_ROOT.as_posix().lower()
        self.kind = "ai_workspace" if self.key.startswith(root_ai) else "external_repo"
        if ws.exists():
            self.files = scan_tree(ws)
            self.cls = classify_files(self.files)
            self.tech = detect_tech(ws, self.files)
            self.memory = self._load_memory(ws)
            self.entries = [f[0] for f in self.files
                            if is_engine_file(Path(f[0]).name, f[3])][:6]
        else:
            self.cls = {"code": 0, "doc": 0, "media": 0, "other": 0, "total_files": 0,
                        "total_bytes": 0, "langs": [], "language": "—"}
        # 时间跨度
        for s in self.sessions:
            ts = s.get("created")
            te = s.get("updated") or ts
            if ts:
                self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            if te:
                self.last_ts = te if self.last_ts is None else max(self.last_ts, te)
        if not self.first_ts and ws.exists():
            self.first_ts = int(ws.stat().st_ctime * 1000)
        if not self.last_ts and ws.exists():
            self.last_ts = int(ws.stat().st_mtime * 1000)
        # 分类
        code, doc = self.cls["code"], self.cls["doc"]
        if code >= min_code and code >= doc * 0.3:
            self.type = "代码项目"
        elif doc > 0:
            self.type = "文档任务"
        elif self.cls["total_files"] > 0:
            self.type = "混合产物"
        else:
            self.type = "空壳"
        self.name = self._guess_name()
        return self

    def _guess_name(self):
        """预命名：身份证据优先，会话标题只作兜底。

        顺序：git 远端 > 工程清单 > 工作区目录名 > 会话标题 > 语言。
        前三级是「磁盘上的事实」，后两级是「猜测」。旧版把猜测放在第一位，
        于是出现了叫 `https`、`密码为什么为空 LoginPanel` 的项目。
        """
        ws = Path(self.workspace)
        if ws.exists():
            n, src = identity_name(ws, [f[0] for f in self.files])
            if n:
                self.name_src = src
                return safe_name(n)
        titles = [s.get("title") for s in sorted(self.sessions,
                                                 key=lambda x: x.get("created") or 0)]
        t = pick_title([x for x in titles if x])
        if t:
            self.name_src = "会话标题"
            return safe_name(t)
        if self.cls.get("langs"):
            self.name_src = "语言兜底"
            return safe_name(f"{self.cls['langs'][0][0]} 项目")
        self.name_src = "目录名兜底"
        return safe_name(ws.name)

    def _load_memory(self, ws: Path) -> str:
        """读取工作区项目记忆（requirement: 回顾开发过程记忆）。

        记忆目录由来源平台决定；平台无此概念时返回空，不影响其余归档。
        """
        src = self.sessions[0].get("source") if self.sessions else ""
        md = adapter_of(src or "").memory_dir(ws)
        if not md:
            return ""
        md = Path(md)
        if not md.exists():
            return ""
        parts = []
        mem = md / "MEMORY.md"
        if mem.exists():
            parts.append("### 项目长期记忆 (MEMORY.md)\n" + read_text(mem, 3000))
        logs = sorted(md.glob("*.md"), key=lambda p: p.stem, reverse=True)
        logs = [p for p in logs if p.name != "MEMORY.md"][:6]
        for p in reversed(logs):
            txt = read_text(p, 1600)
            if txt.strip():
                parts.append(f"### 日志 {p.stem}\n" + txt.strip())
        out = "\n\n".join(parts)
        return out[:DEFAULTS["max_memory_chars"]]


def refine_name(p: Project, parses: dict):
    """会话标题无效（如 'E' / 'summary'）时，用首条真实提问重新预命名。

    名字若已由身份证据（git/清单/目录名）确定，则不再改写——
    磁盘上的事实优先于对话里的措辞。
    """
    if p.name_src and p.name_src != "会话标题":
        return
    if p.name and not is_junk_title(p.name):
        return
    for s in p.sessions:
        pr = parses.get(s["sid"]) or {}
        for x in (pr.get("prompts") or [])[:2]:
            t = short_title(x, 28)
            if len(t) >= 4 and not is_junk_title(t):
                p.name = safe_name(t)
                return


# ================================================================ 状态管理
class State:
    def __init__(self, target: Path):
        self.path = target / STATE_FILE
        self.data = {"version": 1, "target": str(target), "last_run": "",
                     "projects": {}}
        if self.path.exists():
            try:
                self.data = json.loads(read_text(self.path) or "{}")
                self.data.setdefault("projects", {})
                self.data.setdefault("version", 1)
            except Exception:
                pass

    def save(self):
        self.data["last_run"] = iso(now_ts())
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def proj(self, key) -> dict:
        return self.data["projects"].setdefault(key, {})


class Lock:
    """简单跨进程互斥，避免定时任务与手动触发打架"""

    def __init__(self, target: Path):
        self.path = target / ".state.lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                self.fh = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fh, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = now_ts() - self.path.stat().st_mtime
                except Exception:
                    age = 0
                if age > 3600:  # 陈旧锁，1 小时后强制接管
                    try:
                        self.path.unlink()
                    except Exception:
                        pass
                    continue
                time.sleep(0.4)
        self.fh = None
        return self

    def __exit__(self, *a):
        if self.fh is not None:
            try:
                os.close(self.fh)
            except Exception:
                pass
            try:
                self.path.unlink()
            except Exception:
                pass


# ================================================================ 采集阶段
def collect_projects(idx, min_code: int, only: str = "", limit: int = 0,
                     dir_map: dict = None):
    """把会话按 workspace 归并成项目单元"""
    groups = {}
    for sid in sorted(idx.ids()):
        info = idx.info(sid)
        cwd = (info.get("cwd") or "").replace("\\", "/")
        if not cwd:
            continue
        key = cwd.rstrip("/").lower()
        groups.setdefault(key, {"workspace": cwd, "sessions": []})
        groups[key]["sessions"].append({
            "sid": sid,
            "source": info.get("source") or "",
            "title": info.get("title") or "",
            "created": info.get("created"),
            "updated": info.get("updated"),
            "automation": info.get("automation", False),
            "has_transcript": bool(idx.transcript(sid)),
        })
    projects = []
    for key, g in groups.items():
        p = Project(g["workspace"])
        p.sessions = sorted(g["sessions"], key=lambda x: x.get("created") or 0)
        p.compute(min_code)
        if only:
            hay = " ".join([p.name, p.workspace,
                            (dir_map or {}).get(key, "")] +
                           [s.get("title") or "" for s in p.sessions]).lower()
            if only.lower() not in hay:
                continue
        projects.append(p)
    projects.sort(key=lambda x: (x.first_ts or 0))
    if limit:
        projects = projects[-limit:]
    return projects


def build_evidence(p: Project, parses: dict) -> str:
    """生成证据包（喂给大模型的唯一输入，体积受控）"""
    L = []
    L.append(f"# 证据包：{p.name}")
    L.append("")
    L.append("## 1. 基本信息")
    L.append(f"- 工作区路径：`{p.workspace}`")
    L.append(f"- 来源类型：{'AI 从零创建的工作区' if p.kind == 'ai_workspace' else '既有代码仓库'}")
    L.append(f"- 会话数：{len(p.sessions)}（其中自动化会话 "
             f"{sum(1 for s in p.sessions if s.get('automation'))} 个）")
    L.append(f"- 时间跨度：{iso(p.first_ts)} → {iso(p.last_ts)}")
    if p.files:
        L.append(f"- 文件：{p.cls['total_files']} 个 / {human_size(p.cls['total_bytes'])}；"
                 f"源码 {p.cls['code']}、文档 {p.cls['doc']}、媒体 {p.cls['media']}")
    L.append(f"- 语言分布：" + ("、".join(f"{l}({c})" for l, c in p.cls.get("langs", [])[:5]) or "—"))
    if p.tech:
        L.append("- 技术栈信号：" + "；".join(p.tech))
    L.append("")

    L.append("## 2. 会话清单")
    L.append("| 时间 | 标题 | 轮次 | 工具调用(前3) |")
    L.append("|---|---|---|---|")
    for s in p.sessions[:20]:
        pr = parses.get(s["sid"], {})
        tools = sorted((pr.get("tool_counts") or {}).items(), key=lambda x: -x[1])[:3]
        ts = " ".join(f"{k}×{v}" for k, v in tools) or "—"
        L.append(f"| {day(s.get('created'))} | {s.get('title') or '(未命名)'} | "
                 f"{pr.get('turns', 0)} | {ts} |")
    L.append("")

    # 原始需求（取最早会话的首条真实提问）
    req = []
    for s in p.sessions:
        pr = parses.get(s["sid"], {})
        if pr.get("prompts"):
            req.append(pr["prompts"][0])
    if req:
        L.append("## 3. 原始需求（首条用户提问）")
        L.append("")
        L.append("> " + desensitize(req[0][:800]).replace("\n", "\n> "))
        L.append("")

    # 后续关键需求 / 纠偏
    later = []
    for s in p.sessions:
        pr = parses.get(s["sid"], {})
        for x in (pr.get("prompts") or [])[1:4]:
            if 6 <= len(x) <= 300:
                later.append(x)
    if later:
        L.append("## 4. 后续需求与调整（用户逐轮追加）")
        for i, x in enumerate(later[:10], 1):
            L.append(f"{i}. {desensitize(x)[:220]}")
        L.append("")

    # 关键决策片段
    dec = []
    for s in p.sessions:
        for d in parses.get(s["sid"], {}).get("decisions", []):
            if not any(d[:30] == y[:30] for y in dec):
                dec.append(d)
    if dec:
        L.append("## 5. 关键决策与技术说明（AI 回复中提取）")
        for d in dec[:DEFAULTS["max_snippets"]]:
            L.append(f"- {desensitize(d)[:260]}")
        L.append("")

    # 工具画像
    agg = {}
    for s in p.sessions:
        for k, v in (parses.get(s["sid"], {}).get("tool_counts") or {}).items():
            agg[k] = agg.get(k, 0) + v
    if agg:
        top = sorted(agg.items(), key=lambda x: -x[1])[:8]
        L.append("## 6. 开发过程画像")
        L.append("工具调用合计：" + "、".join(f"{k} {v}" for k, v in top))
        total = sum(agg.values())
        L.append(f"总计 {total} 次调用，其中写盘类 "
                 f"{agg.get('Write', 0) + agg.get('Edit', 0)} 次")
        L.append("")

    # 报错与修复
    errs = []
    for s in p.sessions:
        for e in parses.get(s["sid"], {}).get("errors", []):
            if not any(e[:40] == y[:40] for y in errs):
                errs.append(e)
    if errs:
        L.append("## 7. 过程报错线索")
        for e in errs[:8]:
            L.append(f"- {desensitize(e)[:200]}")
        L.append("")

    if p.memory:
        L.append("## 8. 项目记忆摘录")
        L.append(desensitize(p.memory))
        L.append("")

    # 文件清单
    if p.files:
        L.append("## 9. 文件清单（关键文件）")
        shown = [f for f in p.files if f[3] in CODE_EXT or f[0].lower().endswith(("readme.md", "manifest.json"))]
        shown = (shown or p.files)[:60]
        L.append("```")
        for rel, size, mt, ext in shown:
            L.append(f"{rel}  ({human_size(size)})")
        if len(p.files) > len(shown):
            L.append(f"... 另有 {len(p.files) - len(shown)} 个文件")
        L.append("```")
        L.append("")

    out = "\n".join(L)
    cap = DEFAULTS["max_evidence_chars"]
    if len(out) > cap:
        out = out[:cap] + "\n\n...(证据已截断)"
    return out


# ================================================================ 提取阶段
DOC_VERSION_PAT = re.compile(
    r"[_\-\s（(]*(v\d+(\.\d+)?|final|final\d*|updated|new|old|修订|定稿|终稿|"
    r"新版|旧版|副本|备份|\d{8}|\d{6})[)）_\-\s]*$", re.I)


def doc_base_key(name: str) -> str:
    """去掉文档名末尾的版本标记，得到用于判重的基名"""
    stem = Path(name).stem
    for _ in range(4):
        new = DOC_VERSION_PAT.sub("", stem).strip()
        if new == stem or not new:
            break
        stem = new
    return re.sub(r"[\s_\-（）()]+", "", stem).lower()


def copy_sources(p: Project, dest: Path, docs_dest: Path, stats: dict,
                 ov: dict = None):
    """按分级策略复制：源码→source/，设计文档→_docs/，资源按体积取舍。

    归档目标是一个「可直接 git 化、他人 clone 后能继续开发」的纯源码目录，
    因此构建产物、依赖包、日志、报告输出与一次性脚本全部剔除；
    体积较大的运行时资源仅登记到 manifest，不复制。
    """
    src_root = Path(p.workspace)
    if not src_root.exists():
        return
    cap = DEFAULTS["max_copy_mb"] * 1048576
    cap_docs = DEFAULTS["max_docs_mb"] * 1048576
    max_res_kb = DEFAULTS["max_resource_kb"]
    max_file = DEFAULTS["max_file_mb"] * 1048576
    keep_scripts = {str(x).replace("\\", "/").lower()
                    for x in ((ov or {}).get("keep_scripts") or [])}

    # 设计文档去重：同一基线的多版本（V6 / V6_final / V6_updated）只留最新一份
    doc_latest = {}
    for rel, size, mtime, ext in p.files:
        if ext not in OFFICE_EXT:
            continue
        key = (str(PurePosixPath(rel).parent).lower(), doc_base_key(Path(rel).name))
        cur = doc_latest.get(key)
        if cur is None or mtime > cur[1]:
            doc_latest[key] = (rel, mtime)
    docs_keep = {v[0].lower() for v in doc_latest.values()}

    for rel, size, mtime, ext in p.files:
        sp = src_root / rel
        if is_sensitive(sp):
            stats["skipped_secret"] += 1
            continue
        name = Path(rel).name
        kind = classify_for_archive(rel, size, name, ext, max_res_kb)

        # 一次性脚本：人工白名单可放行
        if kind == "script" and not (
                rel.lower() in keep_scripts or name.lower() in keep_scripts):
            stats["excluded_scripts"].append(
                {"path": rel, "size": size, "reason": "对话过程中产生的一次性脚本"})
            continue
        if kind == "script":
            kind = "source"

        if kind == "noise":
            stats["excluded_noise"] += 1
            continue

        # 较大资源/数据：登记但不复制，保证目录只留源码
        if kind == "bigresource":
            stats["external"].append(
                {"path": rel, "size": size,
                 "reason": f"资源/数据超过 {max_res_kb}KB，未纳入源码副本"})
            continue

        # 设计文档另存 _docs/
        if kind == "docs":
            if rel.lower() not in docs_keep:
                stats["external"].append(
                    {"path": rel, "size": size, "reason": "同一文档的旧版本，已保留最新版"})
                continue
            if size > max_file:
                stats["external"].append(
                    {"path": rel, "size": size, "reason": "文档超过单文件上限"})
                continue
            if stats["docs_bytes"] + size > cap_docs:
                stats["hit_cap"] = True
                stats["external"].append(
                    {"path": rel, "size": size, "reason": "触碰文档体积上限"})
                continue
            dp = docs_dest / rel
            try:
                dp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dp)
                stats["docs"] += 1
                stats["docs_bytes"] += size
                stats["docs_list"].append(rel)
            except Exception:
                stats["copy_failed"] += 1
            continue

        # 资源：超出阈值只登记，避免把模型/大图塞进源码目录
        if kind == "resource" and size > max_res_kb * 1024:
            stats["external"].append(
                {"path": rel, "size": size,
                 "reason": f"运行时资源超过 {max_res_kb}KB，请从原工作区获取"})
            continue
        if size > max_file:
            stats["external"].append(
                {"path": rel, "size": size, "reason": "单文件超过上限"})
            continue
        if stats["copied_bytes"] + size > cap:
            stats["hit_cap"] = True
            stats["external"].append(
                {"path": rel, "size": size, "reason": "触碰单项目源码体积上限"})
            continue

        dp = dest / rel
        try:
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dp)
            stats["copied"] += 1
            stats["copied_bytes"] += size
            stats["copied_list"].append(rel)
        except Exception:
            stats["copy_failed"] += 1


def recover_deleted(p: Project, parses: dict, dest: Path, stats: dict,
                    ov: dict = None):
    """从转录的 Write 调用中恢复磁盘上已不存在的**工程内源码**。

    只恢复位于工程子目录中的源码文件（如 Services/ColorCorrection.cs），
    根目录的一次性脚本、日志、文档一律不恢复。
    """
    existing = {rel.lower() for rel, *_ in p.files}
    budget = DEFAULTS["max_recovered_kb"] * 1024
    used = 0
    for sid, pr in parses.items():
        for rel, content in (pr.get("written") or {}).items():
            rel = rel.replace("\\", "/")
            if rel.lower() in existing:
                continue
            if rel.startswith(".workbuddy/") or rel.startswith("./.workbuddy/"):
                continue
            name = Path(rel).name
            ext = Path(rel).suffix.lower()
            # 必须在工程子目录内，且是源码/工程配置
            if "/" not in rel.strip("./"):
                continue
            # 路径任一级命中忽略目录（_optdb/_tmp*/bin/node_modules…）则不恢复
            if any(should_skip_dir(seg) for seg in Path(rel).parts[:-1]):
                continue
            if ext not in CODE_EXT and not is_engine_file(name, ext):
                continue
            if is_temp_script(rel, name):
                continue
            b = len(content.encode("utf-8", "replace"))
            if used + b > budget:
                return
            dp = dest / rel
            try:
                dp.parent.mkdir(parents=True, exist_ok=True)
                if not dp.exists():
                    atomic_write(dp, content)
                    used += b
                    stats["recovered"] += 1
                    stats["recovered_list"].append(rel)
            except Exception:
                pass


def copy_memory(p: Project, dest: Path):
    """把工作区项目记忆另存一份，便于日后回溯（目录由来源平台决定）"""
    src = p.sessions[0].get("source") if p.sessions else ""
    md = adapter_of(src or "").memory_dir(Path(p.workspace))
    if not md:
        return 0
    md = Path(md)
    if not md.exists():
        return 0
    n = 0
    total = 0
    for f in sorted(md.glob("*.md")):
        try:
            if total + f.stat().st_size > 2 * 1048576:
                break
            dp = dest / f.name
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dp)
            total += f.stat().st_size
            n += 1
        except Exception:
            pass
    return n


# ---------------------------------------------------------------- 目录修剪
def trash_project(target: Path, dirname: str, reason: str = "") -> bool:
    """把不再归档的项目目录移入 _trash/（软删除，可回滚）"""
    if not dirname:
        return False
    src = target / dirname
    if not src.exists():
        return False
    trash = target / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = trash / f"{dirname}__{stamp}"
    try:
        shutil.move(str(src), str(dst))
        append_changelog(target, "软删除", f"{dirname} → _trash/（{reason}）")
        return True
    except Exception:
        return False


def prune_dir(root: Path, expected: set) -> int:
    """删除 root 下不在 expected 中的文件。

    归档策略收紧后（剔除噪声、临时脚本），旧版本残留的文件需要清理，
    否则目录会一直保留历史垃圾。`.git` 与 `.gitignore` 受保护。
    """
    if not root.exists():
        return 0
    exp = {e.replace("\\", "/").lower() for e in expected}
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dirnames[:] = [d for d in dirnames if d.lower() != ".git"]
        for fn in filenames:
            if fn in (".gitignore",):
                continue
            p = Path(dirpath) / fn
            if ".git" in p.parts:
                continue
            try:
                rel = p.relative_to(root).as_posix().lower()
            except Exception:
                continue
            if rel in exp:
                continue
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
        # 清理空目录
        for d in dirnames:
            if d.lower() == ".git":
                continue
            dp = Path(dirpath) / d
            try:
                if dp.is_dir() and not any(dp.iterdir()):
                    dp.rmdir()
            except Exception:
                pass
    return removed


# ---------------------------------------------------------------- 工程完整性
SLN_PROJ_PAT = re.compile(r'=\s*"([^"]+)"\s*,\s*"([^"]+\.(?:csproj|vcxproj|vbproj|fsproj|shproj))"')
PROJ_COMPILE_PAT = re.compile(r'<\s*Compile\s+Include\s*=\s*"([^"]+)"', re.I)


def _norm_rel(base_rel: str, ref: str) -> str:
    """把工程文件里的相对引用解析为相对项目根的路径（统一 / 分隔）"""
    ref = ref.replace("\\", "/").strip()
    base_dir = PurePosixPath(base_rel).parent
    try:
        return str((base_dir / ref).as_posix()).lstrip("./")
    except Exception:
        return ref


def check_integrity(p: Project, src_dest: Path, stats: dict) -> str:
    """核对归档后的源码能否支撑继续开发，产出 Markdown 报告"""
    present = set()
    if src_dest.exists():
        for rel, *_ in scan_tree(src_dest):
            present.add(rel.lower())

    engines = [rel for rel, *_ in p.files
               if is_engine_file(Path(rel).name, Path(rel).suffix.lower())]
    engines_in = [e for e in engines if e.lower() in present]

    refs = []          # (工程文件, 被引用文件, 是否在源工作区存在)
    for rel in engines:
        ext = Path(rel).suffix.lower()
        if ext not in (".sln", ".csproj", ".vcxproj", ".vbproj", ".fsproj"):
            continue
        text = read_text(Path(p.workspace) / rel, 400000)
        if not text:
            continue
        pat = SLN_PROJ_PAT if ext == ".sln" else PROJ_COMPILE_PAT
        for m in pat.finditer(text):
            raw = m.group(2) if ext == ".sln" else m.group(1)
            # SDK 风格工程常用 **/*.cs 通配，无法逐个核对，跳过
            if "*" in raw or "?" in raw:
                continue
            ref = _norm_rel(rel, raw)
            if not ref or ref.lower() in present:
                continue
            refs.append(ref)

    # 去重并分类：被归档策略剔除的引用单独说明，真正的缺失高亮
    excluded = {x["path"].lower() for x in stats.get("external", [])}
    excluded |= {x["path"].lower() for x in stats.get("excluded_scripts", [])}
    real_missing, dropped = [], []
    for r in dict.fromkeys(refs):
        (dropped if r.lower() in excluded else real_missing).append(r)

    ok = bool(engines_in) and not real_missing
    if engines:
        level = "ok" if ok else "warn"
        headline = ("✅ 源码齐备，可直接继续开发" if ok else "⚠️ 存在缺失，需人工确认")
    else:
        level = "none"
        headline = "ℹ️ 脚本集合，无工程入口文件（无需构建）"
    L = ["# 工程完整性核对", ""]
    L.append(f"- 结论：{headline}")
    L.append(f"- 核对接入时间：{iso(now_ts())}")
    L.append(f"- 工程入口文件：{len(engines_in)} / {len(engines)} 已归档")
    L.append("")

    if engines:
        L.append("## 工程入口")
        L.append("")
        L.append("| 工程文件 | 状态 |")
        L.append("|---|---|")
        for e in engines[:40]:
            L.append(f"| `{e}` | {'已归档' if e.lower() in present else '未归档'} |")
        L.append("")

    if real_missing:
        L.append("## 缺失的引用文件")
        L.append("")
        L.append("以下文件被工程文件引用，但工作区与归档中均不存在，编译/运行会报错：")
        L.append("")
        for r in real_missing[:60]:
            L.append(f"- `{r}`")
        L.append("")
    if dropped:
        L.append("## 已按归档策略剔除的引用")
        L.append("")
        L.append("以下文件体积较大或属运行产物，未纳入源码副本，需从原工作区取回：")
        L.append("")
        for r in dropped[:40]:
            L.append(f"- `{r}`")
        L.append("")
    if not engines:
        L.append("> 未检出工程入口文件（如 .sln / package.json），")
        L.append("> 该项目以脚本或素材为主，无需构建即可直接使用。")
        L.append("")
    return "\n".join(L), level


GITIGNORE = """# ---- 归档器元数据（不属于源码，clone 后按需生成） ----
_evidence/
_summary.json
state.json
*.tmp
打开对话.bat

# ---- 常见构建产物与依赖 ----
bin/
obj/
node_modules/
dist/
build/
out/
target/
packages/
__pycache__/
*.pyc
.venv/
venv/
.vs/
.idea/
.vscode/
*.user
*.suo
TestResults/
coverage/
*.log
"""


# 归档器提交的标记。git-clean 靠它判断「历史是否全由我产生」，比匹配中文文案可靠：
# 文案会改，标记不会。
ARCHIVER_TRAILER = "X-Archiver: dev-project-archivist"
LEGACY_PREFIX = ("init: 归档", "sync: 同步")   # v0.1.x 提交，无 trailer
COMMIT_MAX_ITEMS = 5       # 提交信息最多列 5 条，再多就没人读了
COMMIT_ITEM_LEN = 40       # 单条截断长度（`git log --oneline` 一行放得下）

# 自由文本里的敏感路径检测。SENSITIVE_PATH_HINT 是按「路径列表」写的——要求 .ssh
# 在开头或紧跟 /，而用户写「把 .ssh/config 权限改一下」时前面是空格，锚定版会漏。
SENSITIVE_TEXT_HINT = re.compile(
    r"(?<![A-Za-z0-9_\-])(\.ssh|\.aws|\.kube|\.gnupg|\.docker|credentials)\b"
    r"|(?<![A-Za-z0-9_\-])\.env\b"
    r"|id_rsa|id_ed25519|\.(pem|pfx|p12|key)\b", re.I)

# 平台注入的痕迹，进 commit 没有意义：`@skill:ardot-design-assistant`、`/clear`
SKILL_MARK = re.compile(r"@skill:[\w.\-]+", re.I)
SLASH_CMD = re.compile(r"^/[A-Za-z][\w\-]*$")
# 纯路径 / 纯链接：真实数据里 "F:\" 就是一条指令，它说明不了任何事
BARE_PATH = re.compile(r"^([A-Za-z]:[\\/]|https?://|\\\\|~?/[\w.\-]+/?)")

# 主题挑选：真实数据里最新的一条往往是「问题仍然存在」「继续」「在E盘」这类追问，
# 拿它当主题等于没写。按信息量挑，而不是按时间挑。
SUBJECT_BAD_START = re.compile(
    r"^(继续|还是|仍然|再次|不用|不要|不行|不对|不是|没问题|问题|好了|可以了|先不|"
    r"不动|在|是|对|嗯|哦|好|行|那|这|还有|然后|再试)")
SUBJECT_ACT = re.compile(
    r"(修复|新增|实现|修改|优化|重构|添加|删除|调整|支持|接入|迁移|完成|生成|导入|"
    r"导出|检查|分析|改造|升级|替换|设计|编写|整理|排查|联调|测试|梳理|拆分)")
# 「接着干」类指令：它们只说"继续"，没说继续什么，作主题等于没写
SUBJECT_META = re.compile(
    r"(继续未完成|继续按计划|接着做|按上面的|按上面的思路|继续上一步|继续之前|"
    r"断网|重新发送|再发一次|按计划实施)")
CODE_HINT = re.compile(
    r"[{};]|#define|#include|=>|\b(public|private|protected|void|class|function)\b"
    r"|</\w+>|^\s*//")


def commit_item(t: str) -> str:
    """一条指令 → 一行可进 commit 的短语。返回空串表示这条不该出现。

    三条硬约束：
    - **先脱敏再截断**：反过来的话，token 会被截成半截，正好躲过密钥正则；
    - **只取单行**：多行指令里常贴着代码，塞进 commit 会让 `git log` 变噪声；
    - **提到敏感路径的整条丢弃**：commit 信息会进 git 历史，局部替换成「已隐藏」
      之后语义会变形（"改了 .ssh/config 的权限" 变成 "改了（已隐藏）的权限"），
      不如不要。
    """
    t = desensitize(t or "")
    if len(t) > 400:            # 超长指令先砍，避免对整段代码做正则
        t = t[:400]
    # 顺序要紧：先取第一行再压空白。反过来会把整段拉成一行，多行指令里的代码全跟进来
    line = ""
    for raw in t.splitlines():
        if raw.strip():
            line = raw
            break
    t = " ".join(line.split())
    t = " ".join(SKILL_MARK.sub(" ", t).split())   # 剥掉 @skill:xxx 再判空
    if SENSITIVE_TEXT_HINT.search(t):
        return ""
    t = re.sub(r"^[@#>\s]+", "", t).strip()
    if len(t) < 3:              # "好" "继续" 这种没有信息量；中文三字已是完整语义
        return ""
    if SLASH_CMD.match(t) or BARE_PATH.match(t):
        return ""               # "/clear"、"F:\" 不构成一条工作记录
    if len(t) > COMMIT_ITEM_LEN:
        t = t[:COMMIT_ITEM_LEN].rstrip(" ，。,.、;；") + "…"
    else:
        # "你分析有误：" 这种是引子，后面跟着列表；留着冒号在 commit 里像断了一半
        t = t.rstrip(" ：:，,、")
    return t if len(t) >= 3 else ""


def subject_score(item: str) -> int:
    """给候选主题打分：越像「一件具体做完的事」分越高。

    实测样本里最高分和最低分的差距很大：
        "新增范围配置表 config_scope"            像一件事
        "问题仍然存在" / "在e盘" / "F:\\"        什么都不说明
        "//#define RecordStrokeData 调试用"      是贴进来的代码
    """
    s, n = 0, len(item)
    if 10 <= n <= COMMIT_ITEM_LEN:
        s += 2
    elif n > COMMIT_ITEM_LEN:
        s += 1                  # 被截断的长指令：通常是交代得很细的真任务
    elif 6 <= n < 10:
        s -= 1
    else:
        s -= 3                  # 太短，多半是追问或应答
    if SUBJECT_BAD_START.match(item):
        s -= 2
    if SUBJECT_META.search(item):
        s -= 3                  # "继续未完成的工作"：说了等于没说
    if SUBJECT_ACT.search(item):
        s += 2
    if CODE_HINT.search(item):
        s -= 4
    return s


def sync_subject(name: str, prompts) -> tuple:
    """把「上次提交以来的指令」压成 (主题, 其余条目)。

    主题按信息量挑，不按时间挑——最新的那条常常是「还是不行」。
    没有可用指令时返回 ("", [])，由调用方退回原文案，不编造。
    """
    items, seen = [], set()
    for raw in reversed(list(prompts or [])):     # 最新在前
        it = commit_item(raw)
        if not it or it in seen:                  # 同一句反复问，不该重复占位
            continue
        seen.add(it)
        items.append(it)
    if not items:
        return "", []
    head = max(items, key=subject_score)           # 同分时 max 取第一个 = 最新的
    others = [x for x in items if x != head][:COMMIT_MAX_ITEMS - 1]
    total = min(len(items), COMMIT_MAX_ITEMS)
    if total > 1:
        head = f"{head} 等 {total} 项"
    return head, others


def git_last_commit_ts(adir: Path) -> int:
    """最后一次提交的时间（Unix 秒）。无仓库 / 无提交返回 0。"""
    import subprocess
    if not (adir / ".git").exists():
        return 0
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=str(adir),
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def recent_prompts(p, parses: dict, since_ms: int = 0) -> list:
    """收集 since 之后该项目收到的指令（旧→新）。

    用「上次归档提交的时间」而不是「今天 0 点」划界：定时任务若在 00:05 跑，
    按自然日会把前一天晚上的活全漏掉。这也让漏跑一天后能自动补齐。
    """
    out = []
    for s in p.sessions:
        pr = parses.get(s.get("sid")) or {}
        if since_ms:
            ts = pr.get("last_ts") or s.get("updated") or s.get("created") or 0
            if ts and ts <= since_ms:
                continue
        out.extend(pr.get("prompts") or [])
    return out


def archiver_commits_only(adir: Path) -> bool:
    """仓库历史是否全部由归档器产生（决定 git-clean 能不能安全重建）。"""
    import subprocess
    try:
        r = subprocess.run(["git", "log", "--format=%B%x1e"], cwd=str(adir),
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
    except Exception:
        return False
    recs = [x.strip() for x in (r.stdout or "").split("\x1e") if x.strip()]
    for rec in recs:
        if ARCHIVER_TRAILER in rec or rec.startswith(LEGACY_PREFIX):
            continue
        return False
    return True


def git_init(adir: Path, name: str, prompts=None, note: str = "") -> str:
    """为项目目录初始化独立仓库（幂等）。返回 ok / exists / fail

    prompts 是「上次提交以来」该项目收到的指令（旧→新），用来生成可读的提交信息。
    传空就退回原文案——**宁可信息少，不可编造**。
    """
    import subprocess
    gitdir = adir / ".git"
    if gitdir.exists():
        # 已有仓库：仅在有变化时提交
        try:
            r = subprocess.run(["git", "status", "--porcelain"], cwd=str(adir),
                               capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0 or not (r.stdout or "").strip():
                return "exists"
            changed = len([x for x in (r.stdout or "").splitlines() if x.strip()])
            head, more = sync_subject(name, prompts)
            subject = note or head or f"同步 {name} 归档内容（{changed} 项变化）"
            body = [f"- {x}" for x in more]
            body.append(ARCHIVER_TRAILER)
            msg = f"sync: {subject}\n\n" + "\n".join(body)
            subprocess.run(["git", "add", "-A"], cwd=str(adir), capture_output=True,
                           timeout=120)
            subprocess.run(["git", "-c", "user.name=archiver",
                            "-c", "user.email=archiver@local",
                            "commit", "-q", "-m", msg],
                           cwd=str(adir), capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
            return "committed"
        except Exception:
            return "fail"

    try:
        atomic_write(adir / ".gitignore", GITIGNORE)
        subprocess.run(["git", "init", "-q"], cwd=str(adir), capture_output=True,
                       timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=str(adir), capture_output=True,
                       timeout=180)
        subprocess.run(["git", "-c", "user.name=archiver",
                        "-c", "user.email=archiver@local",
                        "commit", "-q", "-m",
                        f"init: 归档 {name}\n\n{ARCHIVER_TRAILER}"],
                       cwd=str(adir), capture_output=True, text=True,
                       timeout=180, encoding="utf-8", errors="replace")
        # 首次提交后打一次包，避免 loose objects 让 .git 体积虚高
        subprocess.run(["git", "gc", "--quiet", "--prune=now"],
                       cwd=str(adir), capture_output=True, timeout=300)
        return "ok"
    except Exception:
        return "fail"


# ================================================================ 指纹
def project_fingerprint(p: Project, parses: dict) -> str:
    h = hashlib.sha1()
    h.update(p.workspace.lower().encode("utf-8", "replace"))
    for rel, size, mt, ext in p.files:
        h.update(f"{rel}|{size}|{mt};".encode("utf-8", "replace"))
    for s in p.sessions:
        h.update(f"{s['sid']}|{s.get('updated')}|{s.get('title')};".encode("utf-8", "replace"))
    for sid in sorted(parses):
        pr = parses[sid]
        h.update(f"{sid}#{pr.get('turns')}#{len(pr.get('prompts') or [])}".encode())
    return h.hexdigest()


# ================================================================ 扫描主流程
def explain_projects(projects: list, kw: str = "", min_code: int = 0) -> int:
    """打印判定依据：为什么算代码项目、名字取自哪个证据。

    第一次跑的人一定会怀疑「你判得对不对」。让他能问「凭什么」，
    比让判定更准更有效——能被验证的系统才敢信任。
    """
    kw = (kw or "").lower()
    hit = [p for p in projects
           if not kw or kw in p.name.lower() or kw in p.workspace.lower()]

    def conf(p):
        code = p.cls["code"]
        if code >= max(min_code * 3, 20) and p.entries:
            return "高"
        if code >= min_code:
            return "中" if p.entries else "中（无工程入口文件）"
        return "低"

    for p in hit:
        print()
        print(f"{p.name}  →  {p.type}（置信度 {conf(p)}）")
        print(f"  工作区    {safe_path_label(p.workspace)}")
        print(f"  文件      源码 {p.cls['code']}（阈值 {min_code}）· 文档 {p.cls['doc']} "
              f"· 合计 {p.cls['total_files']} 个 / {human_size(p.cls['total_bytes'])}")
        print(f"  入口      {'、'.join(p.entries[:4]) or '（未检出）'}")
        print(f"  命名      {p.name}   ← {p.name_src or '未知'}")
        print(f"  会话      {len(p.sessions)} 个 · {day(p.first_ts)} → {day(p.last_ts)}")
    print()
    if not hit:
        print(f"没有匹配「{kw}」的项目")
    else:
        print(f"共 {len(hit)} 个")
    return 0


def run_scan(target: Path, dry_run=False, limit=0, only="", force=False,
             full_copy=False, min_code=None, verbose=True, sources=None,
             show_all=False, explain=None):
    t0 = now_ts()
    min_code = min_code or DEFAULTS["min_code_files"]
    adapters = get_adapters(sources)
    by_name = {a.name: a for a in adapters}
    idx = build_index(adapters)
    all_ids = idx.ids()
    n_transcript = sum(1 for sid in all_ids if idx.transcript(sid))
    # 阶段编号就是本函数真实的三段：索引 / 归并 / 归档（末尾那次统计属于第三段）。
    # 以前写成 1/5、2/5 再跳到 5/5，中间两段并不存在——这类编号写进 README 会被读者当成截断。
    log(f"[1/3] 会话索引：{len(all_ids)} 个会话，{n_transcript} 份转录")

    # 供 --only 同时匹配归档目录名（state 尚未加载，先轻量读一次）
    _dir_map = {}
    try:
        _st = json.loads(read_text(target / STATE_FILE) or "{}")
        _dir_map = {k: (v.get("dir") or "") for k, v in (_st.get("projects") or {}).items()}
    except Exception:
        pass
    projects = collect_projects(idx, min_code, only=only, limit=limit, dir_map=_dir_map)
    log(f"[2/3] 归并完成：{len(projects)} 个项目单元")

    if dry_run:
        if explain is not None:
            return explain_projects(projects, explain, min_code)
        # 空壳（工作区存在但无源码）默认不列：一张「一半是空壳」的表没人想看，
        # 但它是真实数据，不能藏——用一行说明交代清楚。
        rows = projects if show_all else [p for p in projects if p.type != "空壳"]
        hidden = len(projects) - len(rows)
        # 列宽按终端显示宽度算（CJK 记 2），否则中文名会把表撑歪
        print()
        print(f"{_pad('日期', 11)} {_pad('类型', 9)} {_pad('语言', 15)} "
              f"{_pad('码', 4, True)} {_pad('档', 4, True)} {_pad('文件', 5, True)}  项目")
        print("-" * 99)
        for p in rows:
            label = f"{p.name}  {Path(p.workspace).name}"
            print(f"{_pad(day(p.first_ts), 11)} {_pad(p.type, 9)} "
                  f"{_pad((p.cls.get('language') or '—')[:15], 15)} "
                  f"{_pad(p.cls['code'], 4, True)} {_pad(p.cls['doc'], 4, True)} "
                  f"{_pad(p.cls['total_files'], 5, True)}  {_cut(label, 44)}")
        print("-" * 99)
        cnt = {k: sum(1 for p in projects if p.type == k)
               for k in ("代码项目", "文档任务", "混合产物", "空壳")}
        log(f"小计：代码项目 {cnt['代码项目']} / 文档任务 {cnt['文档任务']} / "
            f"混合产物 {cnt['混合产物']} / 空壳 {cnt['空壳']}")
        if hidden:
            log(f"空壳 {hidden} 个未列入（工作区存在但无源码），加 --all 查看")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    state = State(target)
    touched, skipped, pending = [], 0, []
    added, updated, newly_missing = [], [], []

    ov = load_overrides(target)
    for i, p in enumerate(projects, 1):
        ovp = pick_override(ov, p.key, "")
        if ovp.get("skip"):
            skipped += 1
            continue
        if ovp.get("title"):
            p.name = safe_name(ovp["title"])
        if ovp.get("type") in ("代码项目", "文档任务", "混合产物"):
            p.type = ovp["type"]
        # 只对代码项目与混合产物建归档（文档任务仅在索引中留一行）
        if p.type not in ("代码项目", "混合产物"):
            st = state.proj(p.key)
            # 曾归档过、现已降级（工作区清空或不再含源码）：旧目录移入 _trash
            if st.get("dir") and (target / st["dir"]).exists():
                if trash_project(target, st["dir"], f"已降级为「{p.type}」，不再归档"):
                    st["archived"] = False
            st.update({"type": p.type, "name": p.name, "workspace": p.workspace,
                       "language": p.cls.get("language", "—"),
                       "first_ts": p.first_ts, "last_ts": p.last_ts,
                       "sessions": len(p.sessions), "files": p.cls["total_files"],
                       "bytes": p.cls["total_bytes"], "archived": False,
                       "status": "active" if Path(p.workspace).exists() else "missing"})
            skipped += 1
            continue

        # 解析该项目的所有会话转录
        parses = {}
        for s in p.sessions:
            tp = idx.transcript(s["sid"])
            if not tp:
                continue
            ad = by_name.get(s.get("source") or "") or adapters[0]
            parses[s["sid"]] = SessionParser(tp, p.workspace).parse(ad).summary()

        refine_name(p, parses)

        fp = project_fingerprint(p, parses)
        st = state.proj(p.key)
        if st.get("fingerprint") == fp and not force and (target / st.get("dir", "")).exists():
            st["status"] = "active"
            skipped += 1
            log(f"  [{i}/{len(projects)}] = 无变化 {p.name}")
            continue

        # 目录名：已分配则保持稳定（改名由 render 阶段按摘要标题统一处理）
        is_new = not st.get("dir")
        dirname = st.get("dir")
        if not dirname:
            base = f"{day(p.first_ts) or '0000-00-00'}_{p.name}"
            dirname = base
            n = 2
            while (target / dirname).exists():
                dirname = f"{base}-{n}"
                n += 1
        adir = target / dirname
        adir.mkdir(parents=True, exist_ok=True)

        stats = {"copied": 0, "copied_bytes": 0, "recovered": 0, "skipped_big": 0,
                 "copy_failed": 0, "hit_cap": False, "skipped_secret": 0,
                 "docs": 0, "docs_bytes": 0, "excluded_noise": 0,
                 "excluded_scripts": [], "external": [],
                 "recovered_list": [], "copied_list": [], "docs_list": []}
        src_dir = adir / "source"
        docs_dir = adir / "_docs"
        src_dir.mkdir(parents=True, exist_ok=True)
        ov_one = pick_override(load_overrides(target), p.workspace, dirname)
        copy_sources(p, src_dir, docs_dir, stats, ov_one)
        recover_deleted(p, parses, src_dir, stats, ov_one)
        # 修剪：清掉旧版本遗留的噪声文件（保留本次应收录的全部内容）
        pruned = prune_dir(src_dir, set(stats["copied_list"]) | set(stats["recovered_list"]))
        pruned += prune_dir(docs_dir, set(stats["docs_list"]))
        if not docs_dir.exists() or not any(docs_dir.iterdir()):
            try:
                docs_dir.rmdir()
            except Exception:
                pass
        mem_n = copy_memory(p, adir / META_DIR / "memory")
        integ, integ_level = check_integrity(p, src_dir, stats)
        atomic_write(adir / META_DIR / "integrity.md", integ)

        # 证据包
        ev = build_evidence(p, parses)
        ev_path = adir / EVIDENCE_DIR / "evidence.md"
        atomic_write(ev_path, ev)

        # manifest
        latest = max(p.sessions, key=lambda x: x.get("created") or 0) if p.sessions else None
        manifest = {
            "name": p.name,
            "workspace": p.workspace,
            "kind": p.kind,
            "type": p.type,
            "language": p.cls.get("language", "—"),
            "langs": p.cls.get("langs", [])[:6],
            "tech": p.tech,
            "first_dev": iso(p.first_ts),
            "last_dev": iso(p.last_ts),
            "archived_at": iso(now_ts()),
            # 回原对话：深链由各平台适配器提供，无深链时为空并降级为文字提示
            "open_conversation": (deep_link_of(latest["sid"], latest.get("source") or "")
                                  if latest else ""),
            "open_hint": open_hint_of(latest.get("source") or "") if latest else "",
            "sessions": [{"sid": s["sid"], "title": md_cell(s.get("title"), 120),
                          "created": iso(s.get("created")),
                          "source": s.get("source") or "",
                          "url": deep_link_of(s["sid"], s.get("source") or ""),
                          "automation": s.get("automation", False)} for s in p.sessions],
            "counts": {"turns": sum(x.get("turns", 0) for x in parses.values()),
                       "tool_calls": sum(sum((x.get("tool_counts") or {}).values())
                                         for x in parses.values())},
            "files": {"total": p.cls["total_files"], "code": p.cls["code"],
                      "doc": p.cls["doc"], "bytes": p.cls["total_bytes"]},
            "copied": {"files": stats["copied"], "bytes": stats["copied_bytes"],
                       "recovered": stats["recovered"],
                       "recovered_files": stats["recovered_list"][:100],
                       "hit_cap": stats["hit_cap"], "pruned": pruned},
            "docs": {"files": stats["docs"], "bytes": stats["docs_bytes"]},
            "excluded": {
                "noise": stats["excluded_noise"],
                "scripts": stats["excluded_scripts"][:200],
                "external": stats["external"][:200],
                "secret": stats["skipped_secret"],
            },
            "integrity_level": integ_level,
            "integrity_ok": integ_level in ("ok", "none"),
            "memory_file": META_DIR + "/memory" if mem_n else "",
            "fingerprint": fp,
        }
        atomic_write(adir / MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=2))
        atomic_write(adir / META_DIR / "sessions.json",
                     json.dumps(manifest["sessions"], ensure_ascii=False, indent=2))
        atomic_write(adir / META_DIR / CONVERSATIONS_FILE,
                     build_conversations(p, parses))
        make_open_script(adir, latest["sid"] if latest else "",
                         (latest.get("source") or "") if latest else "")

        # 独立 git 仓库：便于日后提交与他人 clone 继续开发
        # 注意：git 状态不写入 manifest（否则每次提交都会产生 manifest 变更）
        if DEFAULTS["git_enabled"]:
            git_init(adir, p.name,
                     prompts=recent_prompts(p, parses,
                                            git_last_commit_ts(adir) * 1000))

        st.update({
            "dir": dirname, "workspace": p.workspace, "name": p.name, "type": p.type,
            "language": p.cls.get("language", "—"), "first_ts": p.first_ts,
            "last_ts": p.last_ts, "sessions": len(p.sessions),
            "files": p.cls["total_files"], "bytes": p.cls["total_bytes"],
            "fingerprint": fp, "archived": True,
            "last_archived": iso(now_ts()),
            "status": "active",
            "evidence_hash": sha1_text(ev),
        })
        touched.append(p.name)
        (added if is_new else updated).append(p.name)
        need = not (adir / SUMMARY_FILE).exists() or \
            st.get("summary_hash") != st.get("evidence_hash")
        if need:
            pending.append(dirname)
        log(f"  [{i}/{len(projects)}] + {dirname}  "
            f"(源码 {stats['copied']} 个 / {human_size(stats['copied_bytes'])}"
            f"{'，文档 ' + str(stats['docs']) + ' 个' if stats['docs'] else ''}"
            f"{'，复活 ' + str(stats['recovered']) if stats['recovered'] else ''}"
            f"{'，剔除 ' + str(stats['excluded_noise'] + len(stats['excluded_scripts'])) if (stats['excluded_noise'] or stats['excluded_scripts']) else ''}"
            f"{'，清理 ' + str(pruned) if pruned else ''})")

    # 失联检测：state 中有但本次未出现的 workspace
    # 仅在「全量扫描」时判定——用 --only/--limit 局部扫描时，其余项目只是没参与，不是失联
    seen = {p.key for p in projects}
    if not only and not limit:
        for k, st2 in state.data["projects"].items():
            if k not in seen and st2.get("archived"):
                if st2.get("status") != "missing":
                    newly_missing.append(st2.get("name", k))
                st2["status"] = "missing"

    atomic_write(target / "_evidence" / "_pending.json",
                 json.dumps({"pending": pending, "generated_at": iso(now_ts())},
                            ensure_ascii=False, indent=2))
    state.save()
    log(f"[3/3] 扫描完成：更新 {len(touched)} / 跳过 {skipped} / 待归纳 {len(pending)}，"
        f"耗时 {now_ts() - t0:.1f}s")
    return {"added": added, "updated": updated, "removed": newly_missing,
            "pending": pending, "skipped": skipped}


# ================================================================ 渲染阶段
CONVERSATIONS_FILE = "conversations.md"


def open_script_name() -> str:
    """按当前平台给出「打开原对话」脚本的文件名。

    原先固定叫「打开原对话.bat」：非 Windows 上是个废文件，中文名在
    zip 往返、git core.quotepath、部分 CI 下还会乱码。改用 ASCII 名 + 平台后缀。
    """
    if sys.platform.startswith("win"):
        return "open-session.bat"
    if sys.platform == "darwin":
        return "open-session.command"
    return "open-session.sh"


def build_conversations(p: Project, parses: dict) -> str:
    """生成「原始对话索引」：每个会话可一键跳回，并附全部用户指令原文"""
    L = ["# 原始对话索引", ""]
    L.append(f"> 本项目由 {len(p.sessions)} 个会话逐步开发而成。"
             "点击「打开对话」可直接跳回该会话继续开发。")
    L.append("")
    latest = max(p.sessions, key=lambda x: x.get("created") or 0) if p.sessions else None
    src = (latest or {}).get("source") or ""
    L.append(f"> 最近一次会话：`{latest['sid']}`" if latest else "> 未关联会话")
    L.append("")

    ordered = sorted(p.sessions, key=lambda x: x.get("created") or 0)
    for i, s in enumerate(ordered, 1):
        sid = s["sid"]
        title = md_cell(s.get("title"), 120) or "(未命名会话)"
        L.append(f"## {i}. {title}")
        L.append("")
        L.append("| 项 | 值 |")
        L.append("|---|---|")
        L.append(f"| 时间 | {iso(s.get('created')) or '—'} |")
        L.append(f"| 会话 ID | `{sid}` |")
        _link = deep_link_of(sid, s.get("source") or "")
        L.append(f"| 直接打开 | [{_link}]({_link}) |" if _link
                 else f"| 直接打开 | 该平台无深链，{open_hint_of(s.get('source') or '')} |")
        if s.get("automation"):
            L.append("| 备注 | 该会话由自动化任务触发 |")
        L.append("")

        prompts = (parses.get(sid) or {}).get("prompts") or []
        if prompts:
            # 同一句常因重发/重试在转录里出现多次，去重后按首次出现顺序保留
            seen_p, uniq = set(), []
            for t in prompts:
                key = (t or "").strip()
                if not key or key in seen_p:
                    continue
                seen_p.add(key)
                uniq.append(key)
            head = f"**用户指令（{len(uniq)} 条，按时间顺序"
            if len(uniq) != len(prompts):
                head += f"；已合并 {len(prompts) - len(uniq)} 条重复重发"
            head += "）**"
            L.append(head)
            L.append("")
            for j, t in enumerate(uniq[:60], 1):
                t = t.replace("\r", "")
                t = re.sub(r"\n{2,}", "\n", t)
                if len(t) > 500:
                    t = t[:500] + " …"
                # 多行指令转成引用块，保持可读
                first, *rest = t.split("\n")
                L.append(f"{j}. {first}")
                for line in rest:
                    L.append(f"   > {line}")
            if len(uniq) > 60:
                L.append(f"   …（另有 {len(uniq) - 60} 条，可在原会话中查看）")
            L.append("")
    L.append("---")
    L.append("")
    L.append(f"> 跳转方式：{open_hint_of(src)}，或运行项目根目录的 `{open_script_name()}`。")
    L.append("")
    return "\n".join(L)


def make_open_script(adir: Path, latest_sid: str, source: str = "") -> bool:
    """生成「双击即可跳回最近一次原对话」的脚本。

    有深链的平台按当前系统生成 .bat / .command / .sh（内容纯 ASCII，避免编码问题）；
    无深链的平台改生成 open-session.md 说明，避免产出点不开的死链。
    跨平台交出归档时，README 里仍写明深链格式，可手工粘贴。
    """
    if not latest_sid:
        return False
    link = deep_link_of(latest_sid, source)
    if link:
        name = open_script_name()
        if name.endswith(".bat"):
            body = "@echo off\r\nstart \"\" \"%s\"\r\n" % link
        elif name.endswith(".command"):
            body = "#!/bin/sh\nopen \"%s\"\n" % link
        else:
            body = "#!/bin/sh\nxdg-open \"%s\" >/dev/null 2>&1 &\n" % link
        try:
            dest = adir / name
            atomic_write(dest, body)
            if not name.endswith(".bat"):
                try:
                    dest.chmod(0o755)
                except Exception:
                    pass
            return True
        except Exception:
            return False
    ad = adapter_of(source)
    body = ("# 打开原始对话\n\n"
            "本项目由 **%s** 中的会话逐步开发而成，该平台不支持一键跳转。\n\n"
            "- 会话 ID：`%s`\n"
            "- 打开方式：%s\n" % (ad.display or ad.name, latest_sid, ad.open_hint()))
    try:
        atomic_write(adir / "open-session.md", body)
        return True
    except Exception:
        return False


def norm_features(features) -> list:
    """统一 features 结构 → [(模块名, [条目])]

    兼容两种写法：
      ["功能1", "功能2"]                     → [("", [...])]
      [{"module": "导入", "items": [...]}]    → [("导入", [...]), ...]
    """
    if not features:
        return []
    if isinstance(features, dict):
        features = [{"module": k, "items": v} for k, v in features.items()]
    out = []
    plain = []
    for f in features:
        if isinstance(f, dict):
            mod = str(f.get("module") or f.get("name") or "").strip()
            items = f.get("items") or f.get("features") or []
            if isinstance(items, str):
                items = [items]
            items = [str(x).strip() for x in items if str(x).strip()]
            if items:
                out.append((mod, items))
        elif isinstance(f, str) and f.strip():
            plain.append(f.strip())
    if plain:
        out.insert(0, ("", plain))
    return out


def load_summary(adir: Path) -> dict:
    p = adir / SUMMARY_FILE
    if not p.exists():
        return {}
    try:
        d = json.loads(read_text(p) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def effective_summary(adir: Path, cache: dict = None) -> dict:
    """优先用大模型摘要，缺失则用模板兜底（保证索引永远有内容）"""
    if cache is not None and adir in cache:
        return cache[adir]
    s = load_summary(adir)
    if not s:
        m = {}
        mp = adir / MANIFEST_FILE
        if mp.exists():
            try:
                m = json.loads(read_text(mp) or "{}")
            except Exception:
                m = {}
        s = derive_from_manifest(m, read_text(adir / EVIDENCE_DIR / "evidence.md", 60000))
    if cache is not None:
        cache[adir] = s
    return s


def derive_from_manifest(m: dict, evidence: str) -> dict:
    """无大模型时的模板兜底：从 manifest 与证据包拼出可用描述"""
    tech = "；".join(m.get("tech", [])) or "—"
    langs = m.get("language") or "—"
    purpose = ""
    mm = re.search(r"## 3\. 原始需求（首条用户提问）\n\n>\s*(.+?)(?:\n\n|\Z)", evidence, re.S)
    if mm:
        purpose = re.sub(r"\s*\n>\s*", " ", mm.group(1)).strip()[:300]
    features = []
    for line in re.findall(r"^## 4\. 后续需求与调整.*?\n(.*?)(?:\n## |\Z)", evidence, re.S | re.M):
        features = [re.sub(r"^\d+\.\s*", "", l).strip() for l in line.splitlines()
                    if re.match(r"^\d+\.", l.strip())][:6]
    return {
        "title": m.get("name", ""),
        "purpose": purpose or "（待补充：未生成摘要）",
        "features": features,
        "tech_impl": f"主要语言 {langs}；技术栈信号：{tech}。",
        "tech_stack": m.get("tech", []),
        "language": langs,
        "highlights": [],
        "pitfalls": [],
        "status": "",
        "source": "template",
    }


def render_readme(adir: Path, state_proj: dict, ov: dict = None) -> str:
    m = {}
    mp = adir / MANIFEST_FILE
    if mp.exists():
        try:
            m = json.loads(read_text(mp) or "{}")
        except Exception:
            m = {}
    evidence = read_text(adir / EVIDENCE_DIR / "evidence.md", 60000)
    s = effective_summary(adir) or derive_from_manifest(m, evidence)

    name = s.get("title") or m.get("name") or adir.name
    background = (s.get("background") or "").strip()
    purpose = (s.get("purpose") or "").strip()
    # 人工校正优先
    ovp = pick_override(ov or {}, (state_proj or {}).get("workspace"), adir.name)
    if ovp.get("title"):
        name = ovp["title"]
    if ovp.get("purpose"):
        purpose = ovp["purpose"]
    if ovp.get("background"):
        background = ovp["background"]
    features = norm_features(s.get("features"))
    tech_impl = (s.get("tech_impl") or "").strip()
    stack = s.get("tech_stack") or m.get("tech") or []
    hl = s.get("highlights") or []
    pf = s.get("pitfalls") or []
    origin = s.get("source", "llm" if (adir / SUMMARY_FILE).exists() else "template")

    # 用户手写区保留
    old = read_text(adir / "README.md") if (adir / "README.md").exists() else ""
    user_note = ""
    um = re.search(r"<!-- USER-NOTE:BEGIN -->(.*?)<!-- USER-NOTE:END -->", old, re.S)
    if um:
        user_note = um.group(1).strip()

    cp = m.get("copied") or {}
    dc = m.get("docs") or {}
    ex = m.get("excluded") or {}
    sessions = sorted(m.get("sessions") or [], key=lambda x: x.get("created") or "")

    L = []
    L.append(f"# {name}")
    L.append("")
    L.append("<!-- AUTO:BEGIN 以下内容由归档器自动生成，每次同步会覆盖 -->")
    L.append("")

    # ---- 项目背景（简洁明了）----
    L.append("## 项目背景")
    L.append("")
    L.append(background or purpose or "（摘要未生成，可查看 `_evidence/evidence.md` 了解需求原文）")
    L.append("")
    if background and purpose and background.rstrip("。") != purpose.rstrip("。"):
        L.append("**定位**：" + purpose)
        L.append("")

    # ---- 功能说明（按模块分条）----
    if features:
        L.append("## 功能说明")
        L.append("")
        for mod, items in features[:10]:
            if mod:
                L.append(f"### {mod}")
                L.append("")
            for k, it in enumerate(items[:12], 1):
                L.append(f"{k}. {it}")
            L.append("")

    # ---- 技术实现 ----
    L.append("## 技术实现")
    L.append("")
    L.append(tech_impl or "（未生成）")
    L.append("")
    if stack:
        L.append("**技术栈**：" + "、".join(str(x) for x in stack))
        L.append("")
    if hl:
        L.append("## 关键实现要点")
        L.append("")
        for h in hl[:10]:
            L.append(f"- {h}")
        L.append("")
    if pf:
        L.append("## 踩坑与注意事项")
        L.append("")
        for x in pf[:10]:
            L.append(f"- {x}")
        L.append("")

    # ---- 源码说明（可移交性）----
    L.append("## 源码")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 源码文件 | {cp.get('files', 0)} 个 / {human_size(cp.get('bytes', 0))} |")
    if m.get("integrity_level") or m.get("integrity_ok") is not None:
        lvl = m.get("integrity_level") or ("ok" if m.get("integrity_ok") else "warn")
        label = {"ok": "✅ 源码齐备，可直接继续开发",
                 "none": "ℹ️ 脚本集合，无工程入口（无需构建）",
                 "warn": "⚠️ 有缺失，见 `_meta/integrity.md`"}.get(lvl, lvl)
        L.append(f"| 工程完整性 | {label} |")
    gs = "git" if (adir / ".git").exists() else ""
    if gs:
        L.append("| 版本管理 | 已初始化独立 git 仓库 |")
    if dc.get("files"):
        L.append(f"| 设计文档 | `_docs/` {dc['files']} 个 / {human_size(dc.get('bytes', 0))} |")
    if cp.get("recovered"):
        L.append(f"| 从对话恢复 | {cp['recovered']} 个已删除的源码文件 |")
    ex_bits = []
    if ex.get("scripts"):
        ex_bits.append(f"临时脚本 {len(ex['scripts'])} 个")
    if ex.get("noise"):
        ex_bits.append(f"构建产物/日志/截图 {ex['noise']} 个")
    if ex.get("external"):
        ex_bits.append(f"大资源仅登记 {len(ex['external'])} 个")
    if ex_bits:
        L.append("| 已剔除 | " + "、".join(ex_bits) + " |")
    L.append("")
    L.append("`source/` 为纯源代码副本（相对路径与原工作区一致），"
             "不含构建产物与依赖包，克隆后按工程入口文件还原依赖即可继续开发。")
    L.append("")
    if ex.get("scripts"):
        L.append(f"<details><summary>已剔除的临时脚本（{len(ex['scripts'])} 个，"
                 "对话过程产物，原工作区仍保留）</summary>")
        L.append("")
        for x in ex["scripts"][:20]:
            L.append(f"- `{x['path']}`（{human_size(x.get('size', 0))}）")
        if len(ex["scripts"]) > 20:
            L.append(f"- …（另有 {len(ex['scripts']) - 20} 个，完整清单见 `manifest.json`）")
        L.append("")
        L.append("</details>")
        L.append("")
    if ex.get("external"):
        L.append(f"<details><summary>未复制的较大资源（{len(ex['external'])} 个，"
                 "需从原工作区或官方源获取）</summary>")
        L.append("")
        for x in ex["external"][:20]:
            L.append(f"- `{x['path']}`（{human_size(x.get('size', 0))}）— {x.get('reason', '')}")
        if len(ex["external"]) > 20:
            L.append(f"- …（另有 {len(ex['external']) - 20} 个，完整清单见 `manifest.json`）")
        L.append("")
        L.append("</details>")
        L.append("")

    # ---- 原始对话（可一键跳回继续开发）----
    L.append("## 原始对话")
    L.append("")
    L.append(f"本项目由 {len(sessions)} 个会话逐步开发而成，"
             + ("点击「打开」可直接跳回该会话继续开发。" if m.get("open_conversation")
                else "各会话 ID 可用于在对应工具中恢复。"))
    L.append("")
    L.append("| # | 时间 | 会话 | 打开 |")
    L.append("|---|---|---|---|")
    for i, x in enumerate(sessions[:30], 1):
        tag = "（自动化）" if x.get("automation") else ""
        sid = x.get("sid") or ""
        link = x.get("url") or deep_link_of(sid, x.get("source") or "")
        opened = f"[打开对话]({link})" if link else "无深链"
        L.append(f"| {i} | {md_cell(x.get('created'), 16)} "
                 f"| {md_cell(x.get('title'), 48) or '(未命名)'}{tag} "
                 f"| {opened} |")
    L.append("")
    L.append("> 全部用户指令原文（按时间顺序）见 [`_meta/conversations.md`](./_meta/conversations.md)。")
    if m.get("open_conversation"):
        L.append(f"> 也可运行项目根目录的 `{open_script_name()}` 直达最近一次会话。")
    elif m.get("open_hint"):
        L.append(f"> 该平台不支持深链，恢复方式：{m['open_hint']}。")
    L.append("")

    # ---- 概览 ----
    L.append("## 概览")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 首次开发 | {m.get('first_dev') or '—'} |")
    L.append(f"| 最后开发 | {m.get('last_dev') or '—'} |")
    L.append(f"| 归档更新 | {iso((state_proj or {}).get('last_archived')) or iso(now_ts())} |")
    L.append(f"| 类型 | {m.get('type') or '—'} |")
    L.append(f"| 开发语言 | {m.get('language') or '—'} |")
    L.append(f"| 会话数 | {len(sessions)} |")
    L.append(f"| 原工作区文件 | {(m.get('files') or {}).get('total', 0)} 个 "
             f"（{human_size((m.get('files') or {}).get('bytes', 0))}） |")
    L.append(f"| 原工作区 | `{m.get('workspace') or ''}` |")
    L.append(f"| 摘要来源 | {'大模型归纳' if origin == 'llm' else '模板兜底'} |")
    L.append("")
    L.append("<!-- AUTO:END -->")
    L.append("")
    L.append("<!-- USER-NOTE:BEGIN -->")
    if user_note:
        L.append(user_note)
    else:
        L.append("（此处可手写笔记，同步时不会被覆盖）")
    L.append("<!-- USER-NOTE:END -->")
    L.append("")
    return "\n".join(L)


def run_render(target: Path, rename=True, verbose=True):
    if not target.exists():
        log(f"[error] 目标目录不存在：{target}")
        return 2
    state = State(target)
    ov = load_overrides(target)
    rows = []
    changed = 0
    for k, st in state.data["projects"].items():
        if not st.get("archived"):
            continue
        adir = target / st.get("dir", "")
        if not adir.exists():
            st["status"] = "missing"
            continue

        # 大模型摘要产出后，用摘要标题重命名目录（标题变化时才动）
        # 人工改名（name_source=manual）优先：防止下次 run 被 LLM 摘要改回去
        s = load_summary(adir)
        manual_name = st.get("name_source") == "manual"
        if rename and not manual_name and s.get("title") and st.get("name") != s["title"]:
            new_name = safe_name(s["title"])
            old_dir = st.get("dir", "")
            base = f"{day(st.get('first_ts')) or '0000-00-00'}_{new_name}"
            if base != old_dir:
                cand = base
                n = 2
                while (target / cand).exists() and cand != old_dir:
                    if state.data["projects"].get(str((target / cand).as_posix()).lower(),
                                                  {}).get("workspace") == st.get("workspace"):
                        break
                    cand = f"{base}-{n}"
                    n += 1
                try:
                    if cand == base or not (target / cand).exists():
                        os.rename(adir, target / cand)
                        st["dir"] = cand
                        adir = target / cand
                        st["renamed_from"] = old_dir
                except Exception as e:
                    log(f"  [warn] 重命名失败 {old_dir} → {cand}: {e}")
            st["name"] = new_name
            st["name_source"] = "llm"

        txt = render_readme(adir, st, ov)
        rp = adir / "README.md"
        old = read_text(rp) if rp.exists() else ""
        if old != txt:
            atomic_write(rp, txt)
            changed += 1
        # 记录摘要状态
        if s:
            st["summary_hash"] = st.get("evidence_hash")
            st["summary_source"] = s.get("source", "llm")
        rows.append((st, adir))

    rows.sort(key=lambda x: (x[0].get("first_ts") or 0))
    write_index(target, rows, state, ov)
    state.save()

    # README/索引已重写，补一次提交，避免仓库长期停在未提交状态
    # 这里没有转录可读（render 只吃 state），所以给固定主题，不假装知道"做了什么"
    if DEFAULTS["git_enabled"]:
        for st, adir in rows:
            if st.get("dir") and (adir / ".git").exists():
                git_init(adir, st.get("name") or adir.name, note="更新 README 与索引")

    log(f"[render] README 更新 {changed} 个，索引项目 {len(rows)} 个")
    return 0


def write_index(target: Path, rows: list, state: State, ov: dict = None):
    now = iso(now_ts())
    cache = {}
    # 管理字段：置顶优先；取消关注只留标记，不删条目（索引始终是完整账本）
    ov = ov or {}
    pinned = {k.lower() for k, v in ov.items() if not k.startswith("_")
              and isinstance(v, dict) and v.get("pinned")}
    hidden = {k.lower() for k, v in ov.items() if not k.startswith("_")
              and isinstance(v, dict) and v.get("watching") is False}

    def is_pinned(st, adir):
        return (norm_key_manage(st.get("workspace")) in pinned
                or (adir.name or "").lower() in pinned)

    def is_hidden(st, adir):
        return (norm_key_manage(st.get("workspace")) in hidden
                or (adir.name or "").lower() in hidden)

    # 置顶项稳定提前；无置顶时保持原顺序（与基线逐字节一致）
    if pinned:
        rows = sorted(rows, key=lambda x: not is_pinned(x[0], x[1]))

    def nm(st, adir):
        o = pick_override(ov or {}, st.get("workspace"), adir.name)
        return o.get("title") or effective_summary(adir, cache).get("title") \
            or st.get("name") or adir.name

    L = ["# WorkBuddy 对话项目归档总索引", ""]
    L.append(f"> 最后同步：{now}　|　项目总数：{len(rows)}　|　"
             f"归档目录：`{target}`")
    L.append(">")
    L.append("> 本文件由归档器自动生成。项目名、用途等来自对话历史归纳，"
             "源码为工作区快照副本。")
    L.append("")
    L.append("## 项目总览")
    L.append("")
    L.append("| # | 项目 | 首次开发 | 最后开发 | 最后更新 | 类型 | 开发语言 | "
             "会话 | 原文件数 | 用途 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, (st, adir) in enumerate(rows, 1):
        s = effective_summary(adir, cache)
        o = pick_override(ov or {}, st.get("workspace"), adir.name)
        purpose = (o.get("purpose") or s.get("purpose") or "").strip()
        purpose = purpose.replace("\n", " ").replace("|", "／")
        if len(purpose) > 46:
            purpose = purpose[:46] + "…"
        name = nm(st, adir)
        lang = (s.get("language") or st.get("language") or "—").replace("|", "／")
        rel = adir.name
        status = st.get("status")
        mark = "" if status == "active" else " ⚠️失联"
        if is_hidden(st, adir):
            mark += " 🚫已取消关注"
        if is_pinned(st, adir):
            mark += " 📌"
        L.append(f"| {i} | [{name}](./{rel}/README.md){mark} | {day(st.get('first_ts'))} "
                 f"| {day(st.get('last_ts'))} | {day(st.get('last_archived'))} "
                 f"| {st.get('type') or '—'} | {lang} | {st.get('sessions', 0)} "
                 f"| {st.get('files', 0)} | {purpose or '—'} |")
    L.append("")

    # 按语言分组
    by_lang = {}
    for st, adir in rows:
        s = effective_summary(adir, cache)
        lang = s.get("language") or st.get("language") or "未知"
        by_lang.setdefault(lang.split(" / ")[0], []).append(nm(st, adir))
    if by_lang:
        L.append("## 按开发语言分布")
        L.append("")
        L.append("| 语言 | 项目数 | 项目 |")
        L.append("|---|---|---|")
        for lang, names in sorted(by_lang.items(), key=lambda x: -len(x[1])):
            L.append(f"| {lang} | {len(names)} | {'、'.join(names[:8])}"
                     f"{'…' if len(names) > 8 else ''} |")
        L.append("")

    # 时间线
    L.append("## 开发时间线")
    L.append("")
    by_month = {}
    for st, adir in rows:
        d = day(st.get("first_ts")) or "未知"
        s = effective_summary(adir, cache)
        by_month.setdefault(d[:7], []).append(nm(st, adir))
    for mth in sorted(by_month):
        L.append(f"- **{mth}**：{'、'.join(by_month[mth])}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("归档器：`~/.workbuddy/skills/dev-project-archivist`　"
             "同步记录见 [CHANGELOG.md](./CHANGELOG.md)")
    atomic_write(target / INDEX_MD, "\n".join(L))

    # 机器可读
    data = {
        "generated_at": now,
        "total": len(rows),
        "projects": [
            {
                "name": nm(st, adir),
                "dir": st.get("dir"), "type": st.get("type"),
                "language": effective_summary(adir, cache).get("language") or st.get("language"),
                "first_dev": day(st.get("first_ts")), "last_dev": day(st.get("last_ts")),
                "archived_at": day(st.get("last_archived")),
                "sessions": st.get("sessions", 0), "files": st.get("files", 0),
                "status": st.get("status"), "workspace": st.get("workspace"),
                "purpose": (pick_override(ov or {}, st.get("workspace"), adir.name).get("purpose")
                            or effective_summary(adir, cache).get("purpose") or "")[:200],
            }
            for st, adir in rows
        ],
    }
    # 管理字段只在有显式设置时写入，避免改变既有 index.json 结构
    for st, adir in rows:
        ov_one = pick_override(ov or {}, st.get("workspace"), adir.name)
        item = next((x for x in data["projects"] if x.get("dir") == st.get("dir")), None)
        if not item:
            continue
        if ov_one.get("pinned"):
            item["pinned"] = True
        if ov_one.get("watching") is False:
            item["watching"] = False
        if ov_one.get("note"):
            item["note"] = ov_one["note"][:200]
    atomic_write(target / INDEX_JSON, json.dumps(data, ensure_ascii=False, indent=2))


def append_changelog(target: Path, action: str, detail: str):
    p = target / CHANGELOG
    head = "# 同步记录\n\n每次运行追加一条记录。\n\n" if not p.exists() else ""
    if head:
        atomic_write(p, head)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"- `{iso(now_ts())}` {action}：{detail}\n")


# ================================================================ CLI
def cmd_pending(target: Path):
    pend = target / "_evidence" / "_pending.json"
    if not pend.exists():
        log("没有待归纳项目（先运行 scan）")
        return 0
    d = json.loads(read_text(pend) or "{}")
    items = d.get("pending") or []
    log(f"待归纳 {len(items)} 个项目：")
    for x in items:
        print(f"  {target / x / EVIDENCE_DIR / 'evidence.md'}")
    return 0


def cmd_status(target: Path):
    state = State(target)
    ps = state.data.get("projects", {})
    arch = [v for v in ps.values() if v.get("archived")]
    log(f"目标目录：{target}")
    log(f"记录项目 {len(ps)} 个，其中已归档 {len(arch)} 个")
    log(f"上次运行：{state.data.get('last_run') or '从未'}")
    miss = [v for v in arch if v.get("status") == "missing"]
    if miss:
        log(f"失联 {len(miss)} 个：" + "、".join(v.get("name", "") for v in miss[:8]))
    nosum = [v for v in arch if not (target / v.get("dir", "") / SUMMARY_FILE).exists()]
    if nosum:
        log(f"缺摘要 {len(nosum)} 个（可运行 pending 查看）")

    # .git 体积体检：每日提交会长期累积，但自动 gc 会拖长定时任务，交由用户决定。
    total_git, big = 0, []
    for v in arch:
        gd = target / v.get("dir", "") / ".git"
        if not gd.exists():
            continue
        sz = dir_bytes(gd)
        total_git += sz
        if sz >= GIT_WARN_BYTES:
            big.append((sz, v.get("name") or v.get("dir") or ""))
    if total_git:
        log(f"仓库体积：.git 合计 {human_size(total_git)}")
    if big:
        big.sort(reverse=True)
        log(f"体积偏大 {len(big)} 个（>{human_size(GIT_WARN_BYTES)}），建议执行 git gc：")
        for sz, nm in big[:5]:
            log(f"  {human_size(sz):>10}  {nm}")
        log("  在对应归档目录跑 `git gc --prune=now`，或用 `git-clean` 重建仓库")

    # 远端体检：只读 .git/config（不调 git 进程），27 个仓库也就几毫秒。
    # 这里只报「配了几个」，具体待推送多少交给 `sync`——status 要快，sync 可以慢。
    n_remote = 0
    for v in arch:
        d = v.get("dir", "")
        if not d:
            continue
        adir = target / d
        if (adir / ".git").exists() and git_remote_names(adir):
            n_remote += 1
    if n_remote:
        log(f"已配远端 {n_remote} 个仓库（`sync` 看待推送，`sync --push` 执行）")
    return 0


# ================================================================ 检索
# 权重：用户记得的往往是「名字」，其次是「文件名」，模糊场景才靠指令原文。
# 摘要只作兜底——它是模型写的，命中它并不代表用户真在这里干过这件事。
FIND_W = {"name": 10, "file": 3, "prompt": 3, "summary": 1}
FIND_CAP = 6                 # 同类证据的数量饱和上限（见 cmd_find 注释）
FIND_MAX_FILES = 4000        # 单项目文件扫描上限，防超大归档拖慢检索


def find_tokens(q: str) -> list:
    """检索词切分：空白分隔；中文没有空格，整串也算一个 token。"""
    q = (q or "").strip().lower()
    if not q:
        return []
    return [x for x in re.split(r"\s+", q) if x] or [q]


def hit_count(text: str, toks: list) -> int:
    low = (text or "").lower()
    return sum(1 for t in toks if t in low)


def snippet_of(text: str, toks: list, width: int = 44) -> str:
    """截取命中位置附近的片段，避免长指令铺满屏"""
    s = " ".join((text or "").split())
    if not s:
        return ""
    low = s.lower()
    pos = min((low.find(t) for t in toks if t in low), default=-1)
    if pos < 0 or len(s) <= width * 2:
        return s[: width * 2] + ("…" if len(s) > width * 2 else "")
    start = max(0, pos - width // 2)
    end = min(len(s), pos + width)
    return ("…" if start else "") + s[start:end] + ("…" if end < len(s) else "")


def parse_conversations(md_path: Path) -> list:
    """解析 _meta/conversations.md → [{title,time,sid,link,prompts}]。

    这个文件是本工具自己写的，格式稳定，直接按行解析比重新解析转录快两个数量级。
    """
    out, cur = [], None
    for line in read_text(md_path, 4_000_000).splitlines():
        m = re.match(r"^##\s+\d+\.\s*(.*)$", line)
        if m:
            cur = {"title": m.group(1).strip(), "time": "", "sid": "",
                   "link": "", "prompts": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^\|\s*时间\s*\|\s*([^|]*?)\s*\|", line)
        if m:
            cur["time"] = m.group(1)
            continue
        m = re.match(r"^\|\s*会话 ID\s*\|\s*`?([^|`]*)`?\s*\|", line)
        if m:
            cur["sid"] = m.group(1).strip()
            continue
        m = re.match(r"^\|\s*直接打开\s*\|\s*\[[^\]]*\]\(([^)]+)\)", line)
        if m:
            cur["link"] = m.group(1).strip()
            continue
        m = re.match(r"^\d+\.\s(.*)$", line)
        if m:
            cur["prompts"].append(m.group(1).strip())
            continue
        if line.startswith("   > ") and cur["prompts"]:
            cur["prompts"][-1] += " " + line[5:].strip()
    return out


def walk_source(adir: Path, limit: int = FIND_MAX_FILES) -> list:
    """列出 source/ 下的相对路径（不做 stat，只要名字）"""
    src = adir / "source"
    if not src.exists():
        return []
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            try:
                out.append((Path(root) / fn).relative_to(adir).as_posix())
            except Exception:
                continue
            if len(out) >= limit:
                return out
    return out


def open_session(adir: Path, link: str = "") -> bool:
    """打开原对话：优先深链，其次项目里的回场脚本。"""
    import subprocess
    if not link:
        return False
    try:
        if sys.platform == "win32":
            os.startfile(link)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", link], timeout=10)
        else:
            subprocess.run(["xdg-open", link], timeout=10)
        return True
    except Exception:
        return False


def cmd_find(target: Path, query: str, as_json: bool = False,
             show_all: bool = False, open_it: bool = False) -> int:
    """跨项目检索：名字 → 文件名 → 指令原文 → 摘要，全部命中才计入。"""
    idx_path = target / INDEX_JSON
    if not idx_path.exists():
        log(f"没有索引：{idx_path}")
        log("先运行 scan 或 run 生成归档，再用 find 检索")
        return 1
    toks = find_tokens(query)
    if not toks:
        log('用法：archiver.py find <关键词>')
        log('多个词用空格分隔，全部命中才计入，例如：find "发票 解析"')
        return 1

    t0 = time.time()
    try:
        data = json.loads(read_text(idx_path) or "{}")
    except Exception as e:
        log(f"索引损坏：{e}")
        return 1

    need = set(toks)
    hits = []
    hidden_count = 0
    for p in data.get("projects") or []:
        # 管理界面「取消关注」的项目默认不出现在检索结果里；--all 可包含
        if p.get("watching") is False and not show_all:
            hidden_count += 1
            continue
        adir = target / (p.get("dir") or "")
        covered = set()
        score = 0

        # 1) 名字命中：最强信号，按 token 数线性加权
        name_text = f"{p.get('name') or ''} {p.get('dir') or ''}"
        got = {t for t in toks if t in name_text.lower()}
        if got:
            covered |= got
            score += FIND_W["name"] * len(got)

        # 2) 文件名命中：数量做饱和处理。上千个备份文件名不能把评分抬到天上，
        #    否则「命中 1000 个备份文件」会压过「命中项目名」——排序就失去意义。
        f_hits = []
        for rel in walk_source(adir):
            if SENSITIVE_PATH_HINT.search(rel):
                continue            # 敏感路径既不该显示，也不该被检索命中
            low = rel.lower()
            got = [t for t in toks if t in low]
            if got:
                covered |= set(got)
                f_hits.append(rel)
        if f_hits:
            score += FIND_W["file"] + min(FIND_CAP, len(f_hits) - 1)
            # 短路径更像「正经源码」，长路径多半是备份/资源目录
            f_hits.sort(key=lambda x: (len(x), x))

        # 3) 指令原文命中：同样按会话数饱和
        p_snips = []
        for s in parse_conversations(adir / META_DIR / CONVERSATIONS_FILE):
            for pr in s["prompts"]:
                if not [t for t in toks if t in pr.lower()]:
                    continue
                covered |= {t for t in toks if t in pr.lower()}
                p_snips.append({"kind": "指令", "text": snippet_of(pr, toks),
                                "session": s["title"], "time": s["time"],
                                "link": s["link"]})
                break               # 每个会话最多贡献一条，避免同一项目刷屏
        if p_snips:
            score += FIND_W["prompt"] + min(FIND_CAP, len(p_snips) - 1)

        # 4) 摘要只作兜底：它是模型写的，命中它不代表用户真在这里干过
        purpose = p.get("purpose") or ""
        if hit_count(purpose, toks):
            covered |= {t for t in toks if t in purpose.lower()}
            score += FIND_W["summary"]

        if score > 0 and covered >= need:
            hits.append({
                "name": p.get("name") or p.get("dir") or "",
                "dir": p.get("dir") or "", "workspace": p.get("workspace") or "",
                "type": p.get("type") or "", "language": p.get("language") or "",
                "first_dev": p.get("first_dev") or "", "last_dev": p.get("last_dev") or "",
                "sessions": p.get("sessions", 0), "files": p.get("files", 0),
                "score": score,
                "snippets": p_snips[:2] + [{"kind": "文件", "text": rel}
                                           for rel in f_hits[:2]],
            })

    # 先按「最后开发」新→旧，再按分数稳排：同分时最近做过的排前面
    hits.sort(key=lambda x: x.get("last_dev") or "", reverse=True)
    hits.sort(key=lambda x: -x["score"])
    strong = [h for h in hits if h["score"] >= FIND_W["file"]]
    weak = [h for h in hits if h["score"] < FIND_W["file"]]
    shown = hits if show_all else strong
    cost = time.time() - t0

    if as_json:
        print(json.dumps({"query": query, "total": len(hits), "cost_s": round(cost, 3),
                          "hits": shown}, ensure_ascii=False, indent=2))
        return 0 if hits else 1

    print()
    if not hits:
        print(f"没有命中「{query}」")
        if hidden_count:
            print(f"  另有 {hidden_count} 个已取消关注的项目被跳过，加 --all 可一并检索")
        print("  换个更短的词试试；或先确认归档里有这个项目（archiver.py status）")
        return 1

    print(f"{len(hits)} 条命中 · 用时 {cost:.2f}s"
          + (f" · 显示 {len(shown)} 条（弱命中 {len(weak)} 条，加 --all 查看）"
             if weak and not show_all else ""))
    for h in shown:
        print()
        print(f"{h['name']}  ·  {h['first_dev']} → {h['last_dev']}  ·  评分 {h['score']}")
        if h["workspace"]:
            print(f"  工作区  {safe_path_label(h['workspace'])}")
        for sn in h["snippets"]:
            if sn["kind"] == "指令":
                when = (sn.get("time") or "")[:16]
                print(f"  [指令] {when}  {sn['text']}")
            else:
                print(f"  [文件] {_cut(sn['text'], 72)}")

    if weak and not show_all:
        print()
        print(f"  另有 {len(weak)} 条弱命中（只命中摘要，未必真在这里干过），"
              f"加 --all 查看")
    if not shown:
        return 0

    top = shown[0]
    print()
    if open_it:
        link = ""
        for sn in top["snippets"]:
            if sn.get("link"):
                link = sn["link"]
                break
        if open_session(target / top["dir"], link):
            print(f"  已打开：{top['name']}")
        else:
            adir = target / top["dir"]
            print(f"  该会话无深链，手动打开：{adir / open_script_name()}")
    else:
        print(f"  → 直接回场：archiver.py find \"{query}\" --open")
    return 0


STAT_BLOCKS = "▁▂▃▄▅▆▇█"


def human_num(n: int) -> str:
    """紧凑数字：账本一行要放下四个量，写全 1234567 反而看不清量级。"""
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _day_list(end: str, n: int) -> list:
    """最近 n 天（含 end），旧→新。"""
    d = datetime.strptime(end, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _spark(vals: list) -> str:
    """迷你柱：一行看完这周哪几天在干活。全零时用 · 占位，别留空白让人以为是截断。"""
    top = max(vals) if vals else 0
    if not top:
        return "·" * len(vals)
    k = len(STAT_BLOCKS) - 1
    return "".join(STAT_BLOCKS[max(0, min(k, int(round(v / float(top) * k))))]
                   if v > 0 else STAT_BLOCKS[0] for v in vals)


def _group_label(g: dict) -> str:
    """项目显示名：目录名；平台时间戳目录则用会话标题（否则一行全是 `2026-11-18-21-34-40`）。"""
    base = os.path.basename(str(g.get("ws") or "").replace("\\", "/").rstrip("/"))
    if base and not PLATFORM_DIR_PAT.match(base):
        return base
    titles = [s.get("title") for s in g.get("sessions") or [] if s.get("title")]
    return (pick_title(titles) if titles else "") or base or "—"


def cmd_stats(sources=None, days: int = 7, end: str = "", as_json: bool = False) -> int:
    """跨天账本：最近 N 天做了多少、花在哪儿、哪些项目最活跃。

    只读，零写入，不调模型。**不估算成本**：WorkBuddy 的 `providerData.requestModelName`
    常为 `auto`，拿默认单价顶替等于编数字——"模型不明就写未记录"是本项目的一条硬规矩。
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    try:
        dates = _day_list(end, max(1, min(int(days or 7), 366)))
    except Exception:
        print(f"  日期格式应为 YYYY-MM-DD：{end}")
        return 1

    r = collect_range(dates, sources)
    if not r.get("ok"):
        print(f"  {r.get('msg')}")
        return 1

    groups = [g for g in r["groups"].values() if not g.get("is_ext")]
    daily = r["daily"]
    sids = {s["sid"] for g in groups for s in g["sessions"]}
    tok_in = sum(g["tok"]["in"] for g in groups)
    tok_out = sum(g["tok"]["out"] for g in groups)
    tok_cache = sum(g["tok"]["cache_read"] for g in groups)
    prompts = sum(len(g["prompts"]) for g in groups)
    files = sum(len(g["files"]) for g in groups)
    writes = sum(g.get("writes", 0) for g in groups)
    edits = sum(g.get("edits", 0) for g in groups)
    hit_rate = (tok_cache / float(tok_in) * 100) if tok_in else 0.0
    blank = [d["date"] for d in daily if not d["hits"]]
    ranked = sorted(groups, key=lambda g: (-len(g["prompts"]), -len(g["files"])))

    if as_json:
        print(json.dumps({
            "from": dates[0], "to": dates[-1], "days": len(dates),
            "totals": {"prompts": prompts, "sessions": len(sids),
                       "projects": len(groups), "files": files,
                       "writes": writes, "edits": edits,
                       "tok_in": tok_in, "tok_out": tok_out,
                       "cache_read": tok_cache,
                       "cache_rate": round(hit_rate, 1)},
            "daily": daily,
            "projects": [{"ws": g["ws"], "name": _group_label(g),
                          "prompts": len(g["prompts"]), "files": len(g["files"]),
                          "sessions": len(g["sessions"]),
                          "tokens": tok_total(g)} for g in ranked],
            "blank_days": blank,
        }, ensure_ascii=False, indent=2))
        return 0

    span = f"{dates[0]} → {dates[-1]}"
    print()
    print(f"  最近 {len(dates)} 天 · {span}")
    print()
    print(f"  对话 {prompts} · 会话 {len(sids)} · 项目 {len(groups)} · "
          f"改动文件 {files}（写 {writes} / 改 {edits}）")
    if tok_in or tok_out:
        print(f"  Token 输入 {human_num(tok_in)}"
              f"（缓存命中 {hit_rate:.0f}%）· 输出 {human_num(tok_out)}"
              f" · 合计 {human_num(tok_in + tok_out)}")
        print("  成本：不估算（模型归属常为 auto，拿默认单价顶替等于编数字）")
    else:
        print("  Token 未记录：这几天的会话没有 usage 字段")

    print()
    print(f"  每日对话  {_spark([d['prompts'] for d in daily])}")
    print()
    print(f"  {_pad('日期', 12)}{_pad('对话', 5, True)}{_pad('项目', 7, True)}"
          f"{_pad('文件', 7, True)}{_pad('token', 9, True)}")
    print("  " + "-" * 40)
    for d in daily:
        when = d["date"][5:]
        row = (f"  {_pad(when, 12)}{_pad(d['prompts'], 5, True)}"
               f"{_pad(d['projects'], 7, True)}{_pad(d['files'], 7, True)}"
               f"{_pad(human_num(d['tokens']), 9, True)}")
        # 空白天不额外加标记：全零已经说明问题，补位反而让表格缺一角
        print(row)

    if ranked:
        print()
        print("  最活跃项目")
        for g in ranked[:8]:
            label = _cut(_group_label(g), 34)
            print(f"    {len(g['prompts']):>4} 对话 · {len(g['files']):>3} 文件 · "
                  f"{human_num(tok_total(g)):>7} tok   {label}")

    if blank:
        shown = " · ".join(x[5:] for x in blank[:10])
        more = "" if len(blank) <= 10 else f" 等 {len(blank)} 天"
        print()
        print(f"  空白天    {shown}{more}（没有会话）")
    print()
    return 0


# ---------------------------------------------------------------- report (HTML)
#
# 单文件、零脚本、零网络请求：报告是要发出去的（贴群、发推、当首屏截图），
# 任何一处外链都会在别人机器上变成空白或白屏。
# 同时默认**不含工作区路径、不含指令原文**——"能分享"和"敢分享"是两件事。

REPORT_PROJECTS_MAX = 40       # 卡片上限：再多就变成另一个 INDEX.md，没人往下滚
REPORT_HEAT_MIN_DAYS = 10      # 少于 10 天不画热力图：一个格子的日历没有意义


def report_label(g: dict) -> str:
    """报告里的项目名。要发出去，所以先脱敏再判敏感；命中的宁可不显示。"""
    name = desensitize(_group_label(g) or "")
    name = " ".join(name.split())
    if not name:
        return "（未命名项目）"
    if SENSITIVE_TEXT_HINT.search(name) or \
            SENSITIVE_PATH_HINT.search(name.replace("\\", "/")):
        return "（名称含敏感信息，已隐藏）"
    return name[:36]


def _rep(s, n: int = 0) -> str:
    """HTML 转义 + 可选截断。报告里每一段动态文本都必须过这里。"""
    t = " ".join(str(s or "").split())
    if n and len(t) > n:
        t = t[:n].rstrip(" ，。,.、;；:：") + "…"
    return _html.escape(t, quote=True)


def _heat_level(v: int, top: int) -> int:
    if not v or not top:
        return 0
    r = v / float(top)
    if r <= 0.25:
        return 1
    if r <= 0.5:
        return 2
    if r <= 0.75:
        return 3
    return 4


def _rep_heat(daily: list) -> str:
    """日历热力格。按周排（7 行），首日前的空格补齐到它真实的星期位置。"""
    if not daily:
        return ""
    top = max(d["prompts"] for d in daily) or 0
    try:
        offset = datetime.strptime(daily[0]["date"], "%Y-%m-%d").weekday()
    except Exception:
        offset = 0
    cells = ['<i class="blank"></i>'] * offset
    for d in daily:
        lv = _heat_level(d["prompts"], top)
        tip = _html.escape(
            f"{d['date']} · 对话 {d['prompts']} · 文件 {d['files']} · "
            f"{human_num(d['tokens'])} tok", quote=True)
        cells.append(f'<i class="h{lv}" title="{tip}"></i>')
    return '<div class="heat">' + "".join(cells) + "</div>"


def _rep_bars(daily: list, w: int = 720, h: int = 104) -> str:
    """每日对话量。用内联 SVG 而不是 div：高度是数据，不能让布局参与计算。"""
    if not daily:
        return ""
    top = max(d["prompts"] for d in daily) or 0
    slot = w / float(len(daily))
    bw = max(2.0, min(20.0, slot * 0.64))
    rects = []
    for i, d in enumerate(daily):
        v = d["prompts"]
        bh = ((h - 2) * v / float(top)) if top else 0.0
        x = i * slot + (slot - bw) / 2.0
        tip = _html.escape(f"{d['date']} · 对话 {v} · 文件 {d['files']} · "
                           f"{human_num(d['tokens'])} tok", quote=True)
        rects.append(f'<rect x="{x:.1f}" y="{h - bh:.1f}" width="{bw:.1f}" '
                     f'height="{max(bh, 1.6):.1f}" rx="1.2"><title>{tip}</title></rect>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="每日对话量">'
            f'<line x1="0" y1="{h}" x2="{w}" y2="{h}" class="base"/>'
            + "".join(rects) + "</svg>")


def _rep_facts(daily: list, horizontal: bool = False) -> str:
    """热力图旁边的三条事实。热力图只说"哪几天忙"，说不出"忙到什么程度"。

    区间太短时热力图没有意义（一个格子不构成日历），改用横排三卡。
    """
    if not daily:
        return ""
    top = max(daily, key=lambda d: d["prompts"])
    n = len(daily)
    total = sum(d["prompts"] for d in daily)
    active = sum(1 for d in daily if d["hits"])
    rows = [
        ("最忙的一天", f"{top['date']} · {top['prompts']} 条"),
        ("活跃天数", f"{active} / {n} 天"),
        ("平均每天", f"{total / float(n):.1f} 条对话"),
    ]
    cls = "facts h" if horizontal else "facts"
    return f'<dl class="{cls}">' + "".join(
        f'<div><dt>{k}</dt><dd>{_rep(v, 30)}</dd></div>' for k, v in rows) + "</dl>"


def _rep_projects(projects: list) -> str:
    if not projects:
        return '<p class="empty">这段时间没有记录到开发活动。</p>'
    top = max(p["prompts"] for p in projects) or 0
    out = []
    for p in projects[:REPORT_PROJECTS_MAX]:
        pct = (p["prompts"] / float(top) * 100.0) if top else 0.0
        span = p["first"] if p["first"] == p["last"] else f"{p['first']} → {p['last']}"
        meta = (f"{p['sessions']} 会话 · {p['files']} 文件 · "
                f"{human_num(p['tokens'])} tok · {span}")
        out.append('<li><div class="prow">'
                   f'<span class="pn">{_rep(p["name"], 44)}</span>'
                   f'<span class="pv">{p["prompts"]}</span></div>'
                   f'<div class="pbar"><i style="width:{pct:.1f}%"></i></div>'
                   f'<div class="pm">{_rep(meta, 96)}</div></li>')
    tail = ""
    if len(projects) > REPORT_PROJECTS_MAX:
        tail = (f'<p class="empty">另有 {len(projects) - REPORT_PROJECTS_MAX} 个项目未展开'
                f'（共 {len(projects)} 个）</p>')
    return '<ul class="proj">' + "".join(out) + "</ul>" + tail


def report_data(sources=None, days: int = 30, end: str = "") -> dict:
    """报告的输入数据。和 stats 共用 collect_range——同一份口径，不另算一遍。"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    try:
        dates = _day_list(end, max(1, min(int(days or 30), 366)))
    except Exception:
        return {"ok": False, "msg": f"日期格式应为 YYYY-MM-DD：{end}"}

    r = collect_range(dates, sources)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "聚合失败"}

    groups = [g for g in r["groups"].values() if not g.get("is_ext")]
    daily = r["daily"]
    sids = {s["sid"] for g in groups for s in g["sessions"]}
    tok_in = sum(g["tok"]["in"] for g in groups)
    tok_out = sum(g["tok"]["out"] for g in groups)
    tok_cache = sum(g["tok"]["cache_read"] for g in groups)
    ranked = sorted(groups, key=lambda g: (-len(g["prompts"]), -len(g["files"]),
                                           report_label(g)))
    projects = [{
        "name": report_label(g),
        "prompts": len(g["prompts"]),
        "files": len(g["files"]),
        "sessions": len(g["sessions"]),
        "tokens": tok_total(g),
        "first": day(g.get("first")) if g.get("first") else "",
        "last": day(g.get("last")) if g.get("last") else "",
    } for g in ranked]

    return {
        "ok": True, "from": dates[0], "to": dates[-1], "days": len(dates),
        "totals": {
            "prompts": sum(p["prompts"] for p in projects),
            "sessions": len(sids),
            "projects": len(projects),
            "files": sum(len(g["files"]) for g in groups),
            "writes": sum(g.get("writes", 0) for g in groups),
            "edits": sum(g.get("edits", 0) for g in groups),
            "tok_in": tok_in, "tok_out": tok_out,
            "cache_read": tok_cache,
            "cache_rate": (tok_cache / float(tok_in) * 100.0) if tok_in else 0.0,
        },
        "daily": daily, "projects": projects,
        "blank": [d["date"] for d in daily if not d["hits"]],
    }


REPORT_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#fbfaf8;--card:#fff;--ink:#15171c;--muted:#767b86;--line:#e8e5df;--acc:#0f766e;
  --h0:#efece6;--h1:#cfe4df;--h2:#9bc9c1;--h3:#4fa79b;--h4:#0f766e;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1115;--card:#161a20;--ink:#e9ebef;--muted:#8d94a0;--line:#242a33;
        --acc:#2dd4bf;--h0:#1d232b;--h1:#1e3b3a;--h2:#245a56;--h3:#2b7f77;--h4:#2dd4bf}
}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
          "Hiragino Sans GB","Microsoft YaHei",sans-serif;
     -webkit-font-smoothing:antialiased}
main{max-width:760px;margin:0 auto;padding:56px 24px 44px}
.eyebrow{margin:0 0 10px;font-size:11px;letter-spacing:.18em;text-transform:uppercase;
         color:var(--muted)}
h1{margin:0 0 8px;font-size:34px;line-height:1.2;letter-spacing:-.01em}
.sub{margin:0;font-size:13px;color:var(--muted)}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
     border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:32px 0 0}
.kpi div{background:var(--card);padding:15px 14px}
.kv{display:block;font-size:23px;font-weight:600;letter-spacing:-.02em;
    font-variant-numeric:tabular-nums}
.kl{display:block;margin-top:2px;font-size:11px;color:var(--muted)}
section{margin-top:40px}
h2{margin:0 0 14px;font-size:11px;font-weight:600;letter-spacing:.12em;
   text-transform:uppercase;color:var(--muted)}
.heat{display:grid;grid-auto-flow:column;grid-auto-columns:17px;
      grid-template-rows:repeat(7,17px);gap:4px}
.heat i{display:block;width:17px;height:17px;border-radius:3px;background:var(--h0)}
.heat i.blank{background:transparent}
.heat i.h1{background:var(--h1)}.heat i.h2{background:var(--h2)}
.heat i.h3{background:var(--h3)}.heat i.h4{background:var(--h4)}
.split{display:grid;grid-template-columns:auto 1fr;gap:28px;align-items:center}
.cap{margin:8px 0 0;font-size:11px;color:var(--muted)}
.facts{display:grid;gap:9px;margin:0}
.facts div{display:flex;justify-content:space-between;gap:14px;padding-bottom:8px;
           border-bottom:1px dashed var(--line);font-size:12.5px}
.facts div:last-child{border-bottom:0;padding-bottom:0}
.facts dt{color:var(--muted)}
.facts dd{margin:0;font-variant-numeric:tabular-nums}
.facts.h{grid-template-columns:repeat(3,1fr);gap:18px}
.facts.h div{display:block;padding-bottom:0;border-bottom:0}
.facts.h dt{margin-bottom:3px}
.facts.h dd{font-size:17px;font-weight:600;color:var(--ink)}
.chart{color:var(--acc)}
.chart svg{display:block;width:100%;height:auto}
.chart rect{fill:currentColor;opacity:.82}
.chart .base{stroke:var(--line);stroke-width:1;fill:none}
.axis{display:flex;justify-content:space-between;margin-top:8px;font-size:11px;
      color:var(--muted);font-variant-numeric:tabular-nums}
.proj{list-style:none;margin:0;padding:0;display:grid;gap:16px}
.prow{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.pn{font-weight:500;overflow-wrap:anywhere}
.pv{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.pbar{height:4px;margin:7px 0 6px;border-radius:2px;background:var(--h0);overflow:hidden}
.pbar i{display:block;height:100%;border-radius:2px;background:var(--acc)}
.pm{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.empty{margin:14px 0 0;font-size:12px;color:var(--muted)}
footer{display:flex;flex-wrap:wrap;gap:10px 18px;justify-content:space-between;
       margin-top:52px;padding-top:18px;border-top:1px solid var(--line);
       font-size:12px;color:var(--muted)}
footer a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
@media (max-width:560px){
  main{padding:36px 18px 32px}h1{font-size:27px}
  .kpi{grid-template-columns:repeat(2,1fr)}
  .split{grid-template-columns:1fr;gap:20px}
}
"""


def build_report_html(d: dict) -> str:
    """把 report_data 变成一份自包含的 HTML。纯函数：同样输入 → 逐字节相同。"""
    t = d["totals"]
    span = f"{d['from']} → {d['to']}"
    tok_all = t["tok_in"] + t["tok_out"]
    kpis = [
        (human_num(t["prompts"]), "条对话"),
        (str(t["projects"]), "个项目"),
        (str(t["files"]), "个改动文件"),
        (human_num(tok_all) if tok_all else "—", "token"),
    ]
    kpi_html = "".join(f'<div><span class="kv">{_rep(v, 12)}</span>'
                       f'<span class="kl">{_rep(k, 12)}</span></div>'
                       for v, k in kpis)

    notes = []
    if t["tok_in"]:
        notes.append(f"输入 {human_num(t['tok_in'])}"
                     f"（缓存命中 {t['cache_rate']:.0f}%）· 输出 {human_num(t['tok_out'])}")
        notes.append("Token 只统计、不折算成本：模型归属常为 auto，"
                     "按默认单价算等于编数字")
    else:
        notes.append("Token 未记录：这些会话没有 usage 字段")
    if d["blank"]:
        notes.append(f"其中 {len(d['blank'])} 天没有会话")
    note_html = "".join(f'<p class="empty">{_rep(x, 120)}</p>' for x in notes)

    # 短区间不画热力图：一个格子的"日历"只会让人以为页面坏了
    if len(d["daily"]) >= REPORT_HEAT_MIN_DAYS:
        activity = (f'<div class="split"><div>{_rep_heat(d["daily"])}'
                    f'<p class="cap">每格一天，颜色越深对话越多</p></div>'
                    f'{_rep_facts(d["daily"])}</div>')
    else:
        activity = _rep_facts(d["daily"], horizontal=True)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 开发账本 · {_rep(span, 40)}</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<main>
<header>
  <p class="eyebrow">AI 开发账本</p>
  <h1>最近 {d['days']} 天</h1>
  <p class="sub">{_rep(span, 40)} · {t['sessions']} 个会话 · {t['writes']} 次写、{t['edits']} 次改</p>
</header>
<section class="kpi">{kpi_html}</section>
<section>
  <h2>每天的活跃度</h2>
  {activity}
  {note_html}
</section>
<section>
  <h2>每日对话量</h2>
  <div class="chart">{_rep_bars(d['daily'])}</div>
  <div class="axis"><span>{_rep(d['from'], 10)}</span><span>{_rep(d['to'], 10)}</span></div>
</section>
<section>
  <h2>项目（按对话量）</h2>
  {_rep_projects(d['projects'])}
</section>
<footer>
  <span>由 <a href="https://github.com/winrich/dev-project-archivist">
  dev-project-archivist</a> 生成 · 数据来自本机对话转录，未上传任何内容</span>
  <span>不含工作区路径与指令原文</span>
</footer>
</main>
</body>
</html>
"""


def cmd_report(target: Path, sources=None, days: int = 30, end: str = "",
               out: str = "", open_it: bool = False) -> int:
    """写一份单文件 HTML 账本（可截图、可直接发人）。只写这一个文件。"""
    d = report_data(sources, days=days, end=end)
    if not d.get("ok"):
        print(f"  {d.get('msg')}")
        return 1

    text = build_report_html(d)
    out_dir = Path(out) if out else (target / "report")
    path = out_dir / f"report-{d['from']}_{d['to']}.html"
    atomic_write(path, text)

    print(f"已写入 {path}")
    total_tok = d["totals"]["tok_in"] + d["totals"]["tok_out"]
    print(f"  {d['days']} 天 · {d['totals']['projects']} 个项目 · "
          f"{d['totals']['prompts']} 条对话 · "
          + (f"{human_num(total_tok)} token" if total_tok else "Token 未记录"))
    print("  报告不含工作区路径与指令原文，可直接分享")
    if open_it:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            print("  打开失败，请手动打开上面的路径")
    return 0


def _force_rmtree(p: Path):
    """删目录，绕过 Windows 的只读属性。

    git 在 Windows 上会把 .git/objects 下的对象文件标成只读，`shutil.rmtree` 直接抛
    PermissionError（WinError 5），导致 `git-clean` 一个都重建不了。先 chmod 再删。
    """
    def on_err(func, path, exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            raise
    shutil.rmtree(str(p), onerror=on_err)


def cmd_git_clean(target: Path):
    """重建由归档器创建的 git 仓库，清掉历史提交里的大对象。

    只在历史提交全部由归档器产生时才重建（判据是提交体里的 ARCHIVER_TRAILER，
    旧版本提交按 LEGACY_PREFIX 兼容），避免破坏用户自己的提交历史。
    """
    state = State(target)
    done = skip = 0
    for k, st in state.data["projects"].items():
        if not st.get("archived") or not st.get("dir"):
            continue
        adir = target / st["dir"]
        gitdir = adir / ".git"
        if not adir.exists() or not gitdir.exists():
            continue
        if not archiver_commits_only(adir):
            print(f"  跳过（含人工提交）：{st['dir']}")
            skip += 1
            continue
        try:
            _force_rmtree(gitdir)
        except Exception as e:
            print(f"  失败：{st['dir']} {e}")
            skip += 1
            continue
        git_init(adir, st.get("name") or st["dir"])
        print(f"  重建：{st['dir']}")
        done += 1
    print(f"git 重建完成：{done} 个，跳过 {skip} 个")
    return 0


# ================================================================ 归档仓库同步
#
# `run` 每天把归档写进各项目目录的本地仓库，但**提交只留在本机**：从不出门。
# 磁盘一坏，账本跟着没。`sync` 补的就是这一步——把本地提交推到你自己配的远端。
#
# 三条硬约束（README / SKILL 写的是同一段话，改这里记得同步）：
#
#   1. **只推配了 remote 的仓库**。归档器不替你决定备份到哪儿，也不猜 URL；
#      没配就跳过并告诉你怎么配。绝不自动建远端——那等于把源码推去一个
#      你自己都没选过的地方。
#   2. **永不 force**。远端有本地没有的提交（落后/分叉）时只报告，让人手动处理。
#      归档仓库里放的是源码，强推一次就可能把别人的改动抹掉。
#   3. **提交信息不编造**。手动同步没有新对话可依据，就老实写「手动同步（N 项变化）」，
#      不假装成某次对话的产物。
#
# 为什么不是 `run` 的一部分：定时任务每天 00:05 跑，它不该依赖网络。
# 推送失败（断网、凭据过期）会把一次成功的归档变成一次报错的定时任务。


def git_remote_names(adir: Path) -> list:
    """仓库配了哪些远端 [(名字, url)]，按名字排序，origin 优先。

    直接读 `.git/config` 而不调 git：`status` 要对 27 个仓库各查一次，
    文本解析是微秒级，起 27 个进程是秒级。
    """
    cfg = adir / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, name, url = [], "", ""
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r'\[remote\s+"(.+)"\]', line)
        if m:
            if name and url:
                out.append((name, url))
            name, url = m.group(1), ""
            continue
        if line.startswith("url") and "=" in line:
            url = line.split("=", 1)[1].strip()
    if name and url:
        out.append((name, url))
    out.sort(key=lambda x: (x[0] != "origin", x[0].lower()))
    return out


def repo_sync_state(adir: Path) -> dict:
    """一个归档仓库的同步状态。只读，不改任何东西。

    字段：branch/remote/url/upstream/ahead/behind/pending/empty/error。
    ahead = -1 表示「有远端但没有跟踪分支」，此时「待推送」的数量是未知而不是 0——
    把它当 0 会让人以为已经推过了。
    """
    st = {"dir": adir.name, "branch": "", "remote": "", "url": "",
          "upstream": "", "ahead": -1, "behind": 0, "pending": 0,
          "empty": False, "error": ""}

    # 远端先解析：它只是读一个配置文件，而且「配了但状态异常」和「没配」
    # 要给完全不同的提示，不能因为后面某步失败就丢掉这个信息。
    remotes = git_remote_names(adir)
    if remotes:
        st["remote"], st["url"] = remotes[0]

    rc, out, _ = _git_run(adir, ["rev-parse", "--git-dir"], timeout=30)
    if rc != 0:
        st["error"] = "不是可用的 git 仓库"
        return st
    # 用 symbolic-ref 而不是 rev-parse --abbrev-ref：后者在「还没提交过」的仓库上报错，
    # 会被误判成 detached，进而把空仓库说成「状态异常」。
    rc, out, _ = _git_run(adir, ["symbolic-ref", "--short", "HEAD"], timeout=30)
    branch = (out or "").strip() if rc == 0 else ""
    if not branch:
        st["error"] = "处于分离头指针（detached HEAD），不推送"
        return st
    st["branch"] = branch

    rc, out, _ = _git_run(adir, ["rev-list", "-n", "1", "HEAD"], timeout=30)
    if rc != 0 or not (out or "").strip():
        st["empty"] = True          # git init 过但一次都没提交
        return st

    rc, out, _ = _git_run(adir, ["status", "--porcelain"], timeout=60)
    if rc == 0:
        st["pending"] = len([x for x in (out or "").splitlines() if x.strip()])

    rc, out, _ = _git_run(adir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                 "@{u}"], timeout=30)
    if rc == 0 and (out or "").strip():
        st["upstream"] = (out or "").strip()
        rc, out, _ = _git_run(adir, ["rev-list", "--count", "--left-right",
                                     f"{st['upstream']}...HEAD"], timeout=120)
        if rc == 0:
            parts = (out or "").split()
            if len(parts) == 2:
                try:
                    st["behind"], st["ahead"] = int(parts[0]), int(parts[1])
                except ValueError:
                    pass
    elif st["remote"]:
        st["ahead"] = -1             # 有远端但没跟踪分支：状态未知
    return st


def _sync_commit(adir: Path, st: dict, name: str) -> str:
    """把未提交的改动提交掉。返回 '' 表示成功或有话要说之外的正常情况。

    复用 git_init 的提交路径，保证提交体里带 ARCHIVER_TRAILER——
    git-clean 靠它判断「这个仓库的历史是不是全由归档器产生」。
    """
    if not st["pending"]:
        return ""
    res = git_init(adir, name, note=f"手动同步（{st['pending']} 项变化）")
    if res not in ("committed", "exists", "ok"):
        return "提交失败（可能是索引被占用或权限不足）"
    return ""


def _sync_push(adir: Path, st: dict) -> dict:
    """推送一个仓库。**不带任何 force 参数，这是设计而不是疏漏。**"""
    res = {"dir": st["dir"], "branch": st["branch"], "remote": st["remote"],
           "committed": st["pending"], "ok": False, "msg": ""}
    args = ["push"]
    if not st["upstream"]:
        args += ["-u", st["remote"], st["branch"]]
    rc, out, err = _git_run(adir, args, timeout=900)
    combined = ((out or "") + "\n" + (err or "")).strip()
    if rc == 0:
        res["ok"] = True
        return res
    low = combined.lower()
    if "non-fast-forward" in low or "fetch first" in low or "rejected" in low \
            or "diverg" in low:
        res["msg"] = "远端有本地没有的提交，未推送（不会 force）"
    elif "permission denied" in low or "authentication" in low or "403" in low:
        res["msg"] = "认证失败，检查远端凭据"
    elif "could not read" in low or "not found" in low or "does not appear" in low:
        res["msg"] = "远端地址不可用"
    else:
        last = [x for x in combined.splitlines() if x.strip()]
        res["msg"] = (last[-1][:160] if last else "推送失败")
    return res


def _short_url(url: str) -> str:
    """远端地址压成 `host/path`：屏幕上要的是「这是哪儿」，scheme 与用户名是噪声。

    三种写法都见过：`https://user:token@host/a/b.git`、`git@host:a/b.git`、
    `ssh://git@host:2222/a/b.git`（带端口）。最后一种保留端口，否则会读成 `host/2222/a/b`。
    """
    u = (url or "").strip()
    u = re.sub(r"^file://", "", u, flags=re.I)
    if re.match(r"^[A-Za-z]:[\\/]", u) or u.startswith("/"):
        # 本地/网络路径（含 Windows 盘符）。整条路径对读者没意义，仓库名才有。
        return "本地 " + re.split(r"[\\/]", u.rstrip("/\\"))[-1]
    u = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", u)   # 去掉 scheme
    if "@" in u:
        u = u.split("@", 1)[1]                            # 去掉用户名/口令
    m = re.match(r"^([^/:]+):(\d+)(/.*)$", u)             # ssh 带端口
    if m:
        return f"{m.group(1)}:{m.group(2)}{m.group(3)}".rstrip("/")
    if ":" in u and "/" not in u.split(":", 1)[0]:        # scp 风格 host:path
        u = u.replace(":", "/", 1)
    return u.rstrip("/")


def cmd_sync(target: Path, push: bool = False, only: str = "",
             as_json: bool = False) -> int:
    """归档仓库的同步：看有哪些没推，或真的推上去。

    默认只预览（零写入）。`--push` 才提交 + 推送，且只针对配了远端的仓库。
    """
    dirs = archive_dirs(target)
    kw = (only or "").strip().lower()
    if kw:
        dirs = [d for d in dirs if kw in d.name.lower()]

    if not dirs:
        if as_json:
            print(json.dumps({"repos": 0, "with_remote": 0, "rows": []},
                             ensure_ascii=False, indent=2))
            return 0
        print()
        print(f"  {target} 下没有归档仓库" + (f"匹配「{only}」" if kw else ""))
        print("  先跑一次 `run` 建立归档" + ("，或去掉 --only" if kw else ""))
        print()
        return 0 if not kw else 1

    rows = [repo_sync_state(d) for d in dirs]
    with_remote = [r for r in rows if r["remote"]]
    dirty = [r for r in rows if r["pending"]]
    ahead = sum(max(0, r["ahead"]) for r in with_remote)

    if not push:
        if as_json:
            print(json.dumps({"repos": len(rows), "with_remote": len(with_remote),
                              "rows": rows}, ensure_ascii=False, indent=2))
            return 0
        print()
        head = f"  归档仓库 {len(rows)} 个 · 配了远端 {len(with_remote)} 个"
        if dirty:
            head += f" · 有未提交改动 {len(dirty)} 个"
        if ahead:
            head += f" · 待推送 {ahead} 个提交"
        print(head)
        if with_remote:
            print()
            print("  " + _pad("仓库", 26) + _pad("分支", 9) + _pad("远端", 30)
                  + _pad("待提交", 7, right=True) + _pad("待推送", 8, right=True))
            print("  " + "-" * 78)
            for r in with_remote:
                cnt = "未知" if r["ahead"] < 0 else (f"+{r['ahead']}" if r["ahead"] else "-")
                if r["behind"]:
                    cnt += f" 落后{r['behind']}"
                print("  " + _pad(_cut(r["dir"], 26), 26)
                      + _pad(_cut(r["branch"], 9), 9)
                      + _pad(_cut(_short_url(r["url"]), 30), 30)
                      + _pad(r["pending"] or "-", 7, right=True)
                      + _pad(cnt, 8, right=True))
        miss = [r for r in rows if not r["remote"]]
        if miss:
            print()
            print(f"  {len(miss)} 个仓库没配远端（不会推送）。要备份到自己的仓库，"
                  f"在归档目录里各执行一次：")
            print("      git remote add origin <你的仓库地址>")
            print("  之后 `sync --push` 就会带上它们。")
        errs = [r for r in rows if r["error"]]
        if errs:
            print()
            for r in errs[:5]:
                print(f"  跳过 {r['dir']}：{r['error']}")
        print()
        print("  这是预览，没有提交也没有推送。加 --push 才执行。")
        print()
        return 0

    # ---- 真推送
    todo = [r for r in with_remote if not r["error"]]
    skipped = [r for r in rows if not r["remote"] and not r["error"]]
    say = (lambda *a, **k: None) if as_json else print    # --json 时只吐机器可读结果
    say()
    say(f"  推送 {len(todo)} 个仓库（跳过 {len(skipped)} 个未配远端）")
    say()
    done, failed = [], []
    # 状态不正常的（分离头指针、目录根本不是仓库）不能静默漏掉：
    # 静默跳过会让人以为「已经推上去了」。
    for r in rows:
        if r["error"]:
            failed.append({"dir": r["dir"], "msg": r["error"]})
            say(f"  {_pad(_cut(r['dir'], 24), 24)} {r['error']}")
    for r in todo:
        adir = target / r["dir"]
        if r["empty"]:
            failed.append({"dir": r["dir"], "msg": "空仓库（还没有任何提交），跳过"})
            say(f"  {_pad(_cut(r['dir'], 24), 24)} 空仓库，跳过")
            continue
        err = _sync_commit(adir, r, r["dir"])
        if err:
            failed.append({"dir": r["dir"], "msg": err})
            say(f"  {_pad(_cut(r['dir'], 24), 24)} {err}")
            continue
        st = repo_sync_state(adir)          # 提交后分支/上游可能刚建立，重读
        res = _sync_push(adir, st)
        res["committed"] = r["pending"]
        if res["ok"]:
            done.append(res)
            head = f"{r['pending']} 项改动已提交 → " if r["pending"] else "无改动 → "
            say(f"  {_pad(_cut(r['dir'], 24), 24)} {head}{res['branch']} 推送成功")
        else:
            failed.append({"dir": r["dir"], "msg": res["msg"]})
            say(f"  {_pad(_cut(r['dir'], 24), 24)} {res['msg']}")
    say()
    say(f"  推送 {len(done)} 个 · 跳过 {len(skipped)} 个（未配远端）· 失败 {len(failed)} 个")
    if failed:
        say()
        say("  失败的不重试、不回退：归档仓库里的提交还在本地，处理完再跑一次即可。")
    say()
    if as_json:
        print(json.dumps({"pushed": done, "failed": failed,
                          "skipped": [r["dir"] for r in skipped]},
                         ensure_ascii=False, indent=2))
    return 0 if not failed else 1


# ================================================================ 按日恢复
#
# 语义先说清楚（README 里写的是同一段话，改这里记得同步）：
#
#   1. 恢复源只有一个：**各归档仓库自己的 git 历史**。
#      也就是「之前确实归档过的那一天」能回来；归档器从没抓进来的东西回不来——
#      媒体、超过体积上限被剔除的二进制、敏感路径、.gitignore 排除的构建产物，
#      以及**首次归档之前**的任何文件。它们从来没进过 git，凭空变不出来。
#   2. 恢复出来的是「那天结束时归档长什么样」，不是「那天改了什么」。
#      某天没跑归档，那天的快照就不存在，取到的是**更早的最近一次**提交，
#      界面上会把那个提交的真实时间打出来，不假装是当天。
#   3. **永远不写回归档目录**。产物落在 <target>/_restore/<date>/<项目>/。
#      「回到某天」最典型的翻车方式就是覆盖当前归档，这里从根上不给这个机会：
#      恢复只读归档仓库，写入点是一个当天新建的独立目录。
#
# 为什么用 `git archive` 而不是 `git checkout -- .`：
#   checkout 会动工作区和索引，可能把当前归档搞成 detached HEAD、覆盖未提交的
#   手工改动；`git archive` 只读对象库，连工作区都不碰。

RESTORE_DIR = "_restore"
RESTORE_MAX_MB = 800            # 单次恢复总量上限，超过要 --force：几百 MB 的意外没人想要


def _git_run(adir: Path, args: list, timeout: int = 120, binary: bool = False):
    """在归档仓库里跑一条 git 命令。返回 (returncode, stdout, stderr)。

    统一走这里，是为了让"仓库不存在/没有 git/命令超时"三种失败都只表现为
    一个非零返回码，调用方不必到处写 try（这些仓库是工具自己建的，出错时
    需要的是一条能看懂的提示，而不是异常栈）。
    """
    import subprocess
    try:
        r = subprocess.run(["git"] + args, cwd=str(adir), capture_output=True,
                           timeout=timeout,
                           text=not binary,
                           encoding=None if binary else "utf-8",
                           errors=None if binary else "replace")
        out = r.stdout if binary else (r.stdout or "")
        err = r.stderr if binary else (r.stderr or "")
        return r.returncode, out, err
    except Exception as e:
        return 1, (b"" if binary else ""), str(e)


def git_commit_at(adir: Path, until_ts: int) -> dict:
    """仓库在 until_ts（含）之前的最后一个提交，没有则返回 {}。

    返回 {"sha", "ts", "subject", "first_ts"}。

    为什么自己比时间戳、不用 `git log --until=2026-11-15`：
    `--until` 按提交里的时区偏移解释边界，用户机器在 UTC+8、归档器提交带 +0800，
    而 git 内部存的是 UTC——跨时区或夏令时地区，边界那一次提交会被判到隔壁天。
    这里统一换算成本地 Unix 秒再比，口径只有一个。
    """
    if not (adir / ".git").exists():
        return {}
    rc, out, _ = _git_run(adir, ["log", "--format=%H %ct"], timeout=60)
    if rc != 0:
        return {}
    rows = []
    for line in (out or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            rows.append((parts[0], int(parts[1])))
        except ValueError:
            continue
    if not rows:
        return {}
    first_ts = rows[-1][1]          # git log 是新的在前，最后一行的才是首次提交
    for sha, ts in rows:
        if ts <= until_ts:
            return {"sha": sha, "ts": ts, "first_ts": first_ts,
                    "subject": git_subject(adir, sha)}
    return {"sha": "", "ts": 0, "first_ts": first_ts, "subject": ""}


def git_subject(adir: Path, sha: str) -> str:
    """提交标题（首行）。恢复界面上要显示它：让人一眼认出回到的是哪一版。"""
    rc, out, _ = _git_run(adir, ["log", "-1", "--format=%s", sha], timeout=30)
    return (out or "").strip().replace("\n", " ")[:72] if rc == 0 else ""


def git_tree_files(adir: Path, sha: str) -> list:
    """该提交里的文件清单 [(相对路径, 字节数)]。

    只用 `git ls-tree`（读树对象，不解压），所以「先看要恢复多少」的成本是毫秒级。
    """
    rc, out, _ = _git_run(adir, ["ls-tree", "-r", "-l", "--full-tree", sha], timeout=120)
    if rc != 0:
        return []
    files = []
    for line in (out or "").splitlines():
        meta, _, rel = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or not rel:
            continue                # 子模块是 commit 项，不是文件，跳过
        try:
            size = int(parts[3])
        except ValueError:
            size = 0
        files.append((rel, size))
    return files


def git_export(adir: Path, sha: str, dest: Path) -> dict:
    """把某个提交的整棵树展开到 dest（目录需不存在或为空）。

    走 `git archive` → 临时 tar → tarfile 流式解包：不碰工作区、不碰索引。
    解包逐个成员检查，拒绝绝对路径与 `..`——git 本身不会产生这种条目，
    但恢复命令是"把旧数据写到现在"，路径穿越这一层不能靠上游保证。
    """
    import tarfile
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="dpa-restore-")
    try:
        tarp = Path(tmpdir) / "snapshot.tar"
        rc, _, err = _git_run(adir, ["archive", "--format=tar", "-o", str(tarp), sha],
                              timeout=1800)
        if rc != 0 or not tarp.exists():
            return {"ok": False, "msg": (err or "git archive 失败").strip()[:200]}
        dest.mkdir(parents=True, exist_ok=True)
        n = total = 0
        with tarfile.open(str(tarp), "r:") as tf:
            for m in tf:
                rel = m.name.replace("\\", "/")
                pp = PurePosixPath(rel)
                if pp.is_absolute() or ".." in pp.parts:
                    continue
                if not (m.isfile() or m.isdir()):
                    continue            # 软链/设备文件不还原：快照里也用不上
                m.name = rel
                try:
                    tf.extract(m, str(dest), filter="data")     # Python 3.12+
                except TypeError:
                    tf.extract(m, str(dest))                    # 3.8–3.11 没有 filter
                if m.isfile():
                    n += 1
                    total += int(m.size or 0)
        return {"ok": True, "files": n, "bytes": total}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def archive_dirs(target: Path) -> list:
    """归档目录下所有带 .git 的项目目录。

    `_`/`.` 开头的跳过：`_restore`、`_meta`、`_evidence` 都不是项目，
    把它们当仓库恢复只会让人以为归档里有 30 个项目。
    """
    if not target.exists():
        return []
    out = [p for p in target.iterdir()
           if p.is_dir() and not p.name.startswith(("_", "."))
           and (p / ".git").exists()]
    out.sort(key=lambda p: p.name.lower())
    return out


def restore_dates(target: Path, limit: int = 20) -> list:
    """所有归档仓库有提交的本地日期（新→旧）：[(日期, 仓库数, 提交数)]。

    没有 `--date` 时先把这张表打出来。用户问不出「能回到哪天」的时候，
    让他自己去 `git log` 27 次是不合理的。
    """
    seen = {}
    for adir in archive_dirs(target):
        rc, out, _ = _git_run(adir, ["log", "--format=%ct"], timeout=60)
        if rc != 0:
            continue
        for line in (out or "").splitlines():
            try:
                ts = int(line.strip())
            except ValueError:
                continue
            d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            rec = seen.setdefault(d, {"repos": set(), "commits": 0})
            rec["repos"].add(adir.name)
            rec["commits"] += 1
    rows = [(d, len(v["repos"]), v["commits"]) for d, v in seen.items()]
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:limit]


def cmd_restore(target: Path, date_str: str = "", only: str = "", out: str = "",
                write: bool = False, force: bool = False, as_json: bool = False) -> int:
    """把归档恢复到某一天的结束状态（副本形式，不覆盖现有归档）。

    默认**只预览不落盘**：恢复动辄几百 MB，先看清"要拿回哪一版、多少文件"
    再决定，比事后找个地方删掉那些目录便宜得多。加 `--write` 才真正展开。
    """
    if not date_str:
        rows = restore_dates(target)
        if not rows:
            print("  还没有可恢复的快照：归档目录下没有带 git 历史（.git）的项目")
            print(f"  归档目录：{target}")
            return 1
        print()
        print("  可恢复的日期（各归档仓库有提交的日子，新 → 旧）")
        print()
        for d, nrepos, ncommits in rows[:14]:
            print(f"    {d}   {nrepos:>3} 个仓库有提交 · {ncommits:>4} 次提交")
        if len(rows) > 14:
            print(f"    … 另 {len(rows) - 14} 天")
        print()
        print("  用法：restore --date 2026-11-18          只预览，不写任何文件")
        print("        restore --date 2026-11-18 --write  展开到 <target>/_restore/2026-11-18/")
        print("  说明：只能回到「那天确实归档过」的最近一次快照；")
        print("        归档器从没抓过的文件（媒体/超大二进制/敏感路径/首次归档之前）不在快照里。")
        print()
        return 1

    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        print(f"  日期格式应为 YYYY-MM-DD：{date_str}")
        return 1
    # 当天 23:59:59 的本地时间戳：恢复的是"这一天结束时"的状态
    end_ts = int(datetime(d0.year, d0.month, d0.day, 23, 59, 59).timestamp())

    dirs = archive_dirs(target)
    if not dirs:
        print("  归档目录下没有带 git 历史的项目，无法恢复")
        print(f"  归档目录：{target}")
        return 1

    kw = (only or "").lower()
    rows, skipped = [], []
    for adir in dirs:
        if kw and kw not in adir.name.lower():
            continue
        c = git_commit_at(adir, end_ts)
        if not c or not c["sha"]:
            first = c.get("first_ts") or 0
            skipped.append({
                "dir": adir.name,
                "reason": (f"该日之前没有归档记录（首次归档 {day(first)}）" if first
                           else "仓库里没有提交"),
            })
            continue
        files = git_tree_files(adir, c["sha"])
        rows.append({
            "dir": adir.name, "sha": c["sha"], "ts": c["ts"],
            "when": iso(c["ts"]),
            "subject": c["subject"], "files": len(files),
            "bytes": sum(s for _, s in files),
        })

    rows.sort(key=lambda r: -r["bytes"])
    total_files = sum(r["files"] for r in rows)
    total_bytes = sum(r["bytes"] for r in rows)

    # 一个都恢复不了就别建空目录：留下一个只有 _restore.md 的日期文件夹，
    # 下次进来的人会以为"那天归档过、只是没内容"，事实是那天什么都没归档。
    if not rows:
        print()
        if kw:
            print(f"  没有项目匹配 --only {only}")
        else:
            print(f"  {date_str} 之前没有任何项目有归档快照")
            if skipped:
                print(f"    {len(skipped)} 个仓库都还没有提交：{skipped[0]['reason']}")
            print("  换个更晚的日期，或不带 --date 看可恢复的日期")
        print()
        return 1

    if as_json:
        print(json.dumps({
            "date": date_str, "preview": not write,
            "restored": rows,
            "skipped": skipped,
            "totals": {"projects": len(rows), "files": total_files, "bytes": total_bytes},
        }, ensure_ascii=False, indent=2))
        if not write:
            return 0

    else:
        print()
        print(f"  归档快照 {date_str}（当天 23:59:59 之前最后一次提交）")
        print(f"  {'预览，未写任何文件' if not write else '恢复到 ' + str(Path(out or (target / RESTORE_DIR)) / date_str)}")
        print()
        if rows:
            print(f"  {_pad('项目', 30)}{_pad('快照时间', 18)}"
                  f"{_pad('文件', 7, True)}{_pad('体积', 10, True)}")
            print("  " + "-" * 66)
            for r in rows:
                print(f"  {_pad(_cut(r['dir'], 30), 30)}{_pad(r['when'][5:16], 18)}"
                      f"{_pad(r['files'], 7, True)}{_pad(human_size(r['bytes']), 10, True)}")
            print()
            print(f"  小计 {len(rows)} 个项目 · {total_files} 个文件 · "
                  f"{human_size(total_bytes)}")
            print()
            for r in rows[:8]:
                print(f"    {_cut(r['dir'], 26):28} {r['sha'][:8]}  {_cut(r['subject'], 46)}")
            if len(rows) > 8:
                print(f"    … 另 {len(rows) - 8} 个，--json 看全部")
        else:
            print("  这一天没有任何项目有快照")
        if skipped:
            print()
            print(f"  无法恢复 {len(skipped)} 个")
            for s in skipped[:8]:
                print(f"    {_pad(_cut(s['dir'], 26), 28)}{s['reason']}")
            if len(skipped) > 8:
                print(f"    … 另 {len(skipped) - 8} 个")

    if not write:
        print()
        print("  这只是预览。加 --write 才会展开成副本（不会改动归档本身）：")
        print(f"    restore --date {date_str} --write")
        print()
        return 0

    if total_bytes > RESTORE_MAX_MB * 1024 * 1024 and not force:
        print()
        print(f"  拒绝执行：这一次要展开 {human_size(total_bytes)}，"
              f"超过上限 {RESTORE_MAX_MB}MB")
        print("  换个更近的日期，或加 --force 明确接受这个体积")
        return 2

    base = Path(out) if out else (target / RESTORE_DIR)
    dest_root = base / date_str
    if dest_root.exists() and any(dest_root.iterdir()) and not force:
        print()
        print(f"  目标目录已有内容：{dest_root}")
        print("  换 --out，或加 --force 覆盖（只覆盖 _restore 下的副本，不碰归档）")
        return 2

    done, failed = [], []
    print()
    for r in rows:
        dest = dest_root / r["dir"]
        if dest.exists():
            _force_rmtree(dest)          # 仅 _restore 下的副本，归档原文不动
        res = git_export(target / r["dir"], r["sha"], dest)
        if res.get("ok"):
            r["got_files"] = res["files"]
            done.append(r)
        else:
            failed.append((r["dir"], res.get("msg") or "解包失败"))
        print(f"  {'✓' if res.get('ok') else '✗'} {_pad(_cut(r['dir'], 30), 30)}"
              f"{r['files']:>6} 个文件")

    idx = build_restore_index(date_str, done, skipped, failed)
    atomic_write(dest_root / "_restore.md", idx)
    atomic_write(dest_root / "_restore.json", json.dumps({
        "date": date_str, "generated_by": "dev-project-archivist",
        "projects": [{"dir": r["dir"], "commit": r["sha"], "snapshot_at": r["when"],
                      "files": r["files"], "bytes": r["bytes"]} for r in done],
        "skipped": skipped,
        "failed": [{"dir": d, "msg": m} for d, m in failed],
    }, ensure_ascii=False, indent=2))

    print()
    print(f"  已恢复 {len(done)} 个项目到 {dest_root}")
    print(f"    文件 {sum(r.get('got_files', 0) for r in done)} 个 · "
          f"{human_size(sum(r['bytes'] for r in done))}")
    if failed:
        print(f"    失败 {len(failed)} 个：" + "、".join(d for d, _ in failed[:5]))
    print("    这是副本：归档目录本身没有被修改")
    print()
    return 0


def build_restore_index(date_str: str, done: list, skipped: list, failed: list) -> str:
    """恢复目录里的 README：说明每个项目来自哪个提交，以及哪些拿不回来。

    给人看的，所以把「这不是那天的改动，而是那天结束时的样子」写在最前面——
    两者混为一谈，是这类工具最容易被误会的地方。
    """
    L = [f"# 归档快照 {date_str}", "",
         f"> 由 dev-project-archivist `restore` 生成。内容取自各归档仓库在 "
         f"{date_str} 23:59:59 之前**最后一次提交**。", "",
         "> 注意两点：这是「那天结束时归档里长什么样」，不是「那天改了什么」；",
         "> 只有之前归档过的东西才在这里——媒体、超大二进制、敏感路径、",
         "> .gitignore 排除的构建产物，以及首次归档之前的文件，从来不在快照里。", ""]
    if done:
        L += ["| 项目 | 快照时间 | 提交 | 文件 | 体积 |", "|---|---|---|---|---|"]
        for r in done:
            L.append(f"| {r['dir']} | {r['when']} | `{r['sha'][:8]}` "
                     f"{md_cell(r['subject'], 48)} | {r['files']} | "
                     f"{human_size(r['bytes'])} |")
        L.append("")
        L.append(f"合计 {len(done)} 个项目 · "
                 f"{sum(r['files'] for r in done)} 个文件 · "
                 f"{human_size(sum(r['bytes'] for r in done))}")
        L.append("")
    if skipped:
        L += ["## 该日之前没有归档记录", ""]
        L += [f"- **{s['dir']}** — {s['reason']}" for s in skipped]
        L.append("")
    if failed:
        L += ["## 恢复失败", ""]
        L += [f"- **{d}** — {m}" for d, m in failed]
        L.append("")
    L += ["---", "",
          f"回退本身不需要这个副本：归档仓库自带完整历史，"
          f"在任一项目目录里 `git log` / `git checkout <sha> -- .` 即可。", ""]
    return "\n".join(L)


# ================================================================ 日维度聚合
def day_range_ms(date_str: str):
    """本地日期 → [起, 止) 毫秒区间，用于从会话元数据粗筛候选，避免全量解析。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = datetime(d.year, d.month, d.day)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def to_ms(v):
    """秒 / 毫秒 / ISO 字符串统一成毫秒整数；拿不到返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except Exception:
            try:
                return int(datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                return None
    v = float(v)
    return int(v * 1000) if 0 < v < 1e11 else int(v)


def hhmm(ts) -> str:
    s = iso(ts)
    return s[11:16] if len(s) >= 16 else ""


def _dw(s: str) -> int:
    """按终端显示宽度计长（CJK 记 2），用于对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _pad(s, n: int, right: bool = False) -> str:
    s = str(s)
    gap = " " * max(0, n - _dw(s))
    return gap + s if right else s + gap


def _cut(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if _dw(s) <= n else s[: max(1, n - 1)] + "…"


def _km(n) -> str:
    """token 数的紧凑写法：日志要的是量级，不需要精确到个位。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _parent_name(fp: str) -> str:
    """取路径的上一级目录名，用于把「工作区外写入」归到可读的桶里。"""
    parts = [p for p in (fp or "").replace("\\", "/").rstrip("/").split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else "?"


# 会改动文件的工具名；用于核对「工具调用数」与「可定位路径数」的差额
EDIT_TOOL_NAMES = ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch")


class DayCollector:
    """把一个会话的事件流按天分桶，并在每天内部再按「指令」切成轮次。

    与 SessionParser 的差别：SessionParser 只留首末时间，够判定「这个项目最近
    在干什么」，但撑不起「今天做了什么」。这里跟游标走，事件归到它前面最近
    的那个 ts 所属的日期。

    为什么还要轮次：日志的最小单元不是「一天」，而是「一句话 → 它带来的产出」。
    只按天收集只能产出两份平行清单（今天说了什么 / 今天改了哪些文件），写日志
    时还得靠人脑把两边对齐。轮次把这个配对在采集阶段就做掉。

    刻意不保留文件正文——735MB 转录全量走一遍，留正文会直接吃掉内存。
    """

    def __init__(self, ws: str, sid: str, source: str, title: str):
        self.ws, self.sid, self.source, self.title = ws, sid, source, title
        self.cur = None
        self.days = {}          # 日期 -> 桶
        self.untimed = 0        # 前面没有任何 ts 的事件数（无法归日）
        self.write_calls = 0    # Write/Edit 类工具调用数
        self.write_hits = 0     # 其中成功归一为工作区相对路径的个数
        self.external_hits = 0  # 其中落在工作区外的个数
        self.dup = 0            # 被压掉的重复指令数
        self._seen = set()      # 长指令的指纹，用于压重
        self._git_cache = {}    # 目录 -> 仓库名，避免逐文件向上找 .git
        self._root_cache = {}   # 目录 -> 仓库根，用于把区外路径截短

    def _bucket(self, d: str) -> dict:
        b = self.days.get(d)
        if b is None:
            b = {"prompts": [], "files": {}, "tools": {}, "external": {},
                 "first": "", "last": "", "chars": 0, "errors": 0,
                 "wcalls": 0, "whits": 0, "exts": 0,
                 # 轮次视图：turns[i] 是该天第 i 条指令及其产出
                 "turns": [], "tcls": {}, "ccmd": {}, "cur": None,
                 # token 用量：唯一能把「成本」和「产出」连起来的字段
                 "tok": {"in": 0, "out": 0, "cache_read": 0},
                 # 工作区外写入的文件与时间跨度，供 --merge-external 生成独立行
                 "extfiles": {}, "extspan": {}}
            self.days[d] = b
        return b

    def _ext_key(self, fp: str) -> str:
        """工作区外写入的分组键：优先仓库名，退化为目录名。

        按目录名分组会把同一个仓库拆成几条（不同子目录各算一个），按 git 身份
        分组则天然收敛；没有 git 的目录仍退回目录名。
        """
        d = os.path.dirname(str(fp).replace("\\", "/")) or str(fp)
        key = self._git_cache.get(d)
        if key is None:
            gid = git_identity(d)
            key = (gid or {}).get("name") or _parent_name(fp)
            self._git_cache[d] = key
        return key

    def _ext_rel(self, fp: str) -> str:
        """区外文件 → 尽量短的显示名：相对仓库根，退化为「目录/文件名」。

        绝对路径写进日志会把正文冲成一堆路径，且暴露用户名，所以必须截短；
        截短后仍保留一级目录，才能看出「动的是 src 还是 docs」。
        """
        p = str(fp).replace("\\", "/")
        d = os.path.dirname(p) or p
        root = self._root_cache.get(d)
        if root is None:
            root = git_work_tree(d)
            self._root_cache[d] = root
        if root:
            pre = root.replace("\\", "/").rstrip("/") + "/"
            if p.lower().startswith(pre.lower()):
                return p[len(pre):]
        return f"{_parent_name(fp)}/{p.rsplit('/', 1)[-1]}"

    def consume(self, events):
        for ev in events:
            kind = ev.get("kind")
            if kind == EV_TS:
                v = ev.get("value")
                if v:
                    self.cur = v
                    d = day(v)
                    if d:
                        b = self._bucket(d)
                        if not b["first"]:
                            b["first"] = v
                        b["last"] = v
                continue
            d = day(self.cur) if self.cur else ""
            if not d:
                self.untimed += 1
                continue
            b = self._bucket(d)
            tn = b["cur"]
            if kind == EV_PROMPT:
                txt = (ev.get("text") or "").strip()
                if txt:
                    # 上下文压缩会把早先的提问原样重放一遍，长指令按指纹压重；
                    # 「继续」「好」这类短指令保留，它们确实是当天的动作。
                    if len(txt) >= 20:
                        fp = sha1_text(txt)
                        if fp in self._seen:
                            self.dup += 1
                            continue
                        self._seen.add(fp)
                    b["prompts"].append((hhmm(self.cur), txt))
                    tn = {"t": hhmm(self.cur), "prompt": txt, "files": [],
                          "cmds": [], "tcls": {}, "errors": 0, "chars": 0,
                          "tok": {"in": 0, "out": 0, "cache_read": 0}}
                    b["turns"].append(tn)
                    b["cur"] = tn
            elif kind == EV_ASSISTANT:
                n = len(ev.get("text") or "")
                b["chars"] += n
                if tn:
                    tn["chars"] += n
            elif kind == EV_TOOL:
                n = ev.get("name") or ""
                b["tools"][n] = b["tools"].get(n, 0) + 1
                cls = ev.get("tcls") or tool_type(n)
                b["tcls"][cls] = b["tcls"].get(cls, 0) + 1
                if tn:
                    tn["tcls"][cls] = tn["tcls"].get(cls, 0) + 1
                cmd = ev.get("cmd") or ""
                if cmd:
                    cc = cmd_class(cmd)
                    b["ccmd"][cc] = b["ccmd"].get(cc, 0) + 1
                    if tn and len(tn["cmds"]) < 8:
                        tn["cmds"].append((cc, cmd))
                if n in EDIT_TOOL_NAMES:
                    self.write_calls += 1
                    b["wcalls"] += 1
            elif kind == EV_WRITE:
                rel = ev.get("rel")
                if rel:
                    self.write_hits += 1
                    b["whits"] += 1
                    op = ev.get("op") or "write"
                    if b["files"].get(rel) != "write":
                        b["files"][rel] = op
                    if tn is not None and rel not in tn["files"]:
                        tn["files"].append(rel)
                elif ev.get("external"):
                    # 工作区外的写入：不进 files，只按仓库/目录计数
                    self.external_hits += 1
                    b["exts"] += 1
                    k = self._ext_key(ev["external"])
                    b["external"][k] = b["external"].get(k, 0) + 1
                    # 同时留一份文件清单：归档必须丢弃区外写入，日志不能
                    rec = b["extfiles"].setdefault(k, {})
                    rel = self._ext_rel(ev["external"])
                    if rec.get(rel) != "write":
                        rec[rel] = ev.get("op") or "write"
                    if self.cur:
                        sp = b["extspan"].setdefault(k, ["", ""])
                        if not sp[0] or self.cur < sp[0]:
                            sp[0] = self.cur
                        if not sp[1] or self.cur > sp[1]:
                            sp[1] = self.cur
            elif kind == EV_TOOL_RESULT:
                b["errors"] += 1
                if tn:
                    tn["errors"] += 1
            elif kind == EV_USAGE:
                for k in ("in", "out", "cache_read"):
                    v = ev.get(k) or 0
                    if v:
                        b["tok"][k] += v
                        if tn:
                            tn["tok"][k] += v
        return self



def _merge_external_groups(groups: dict):
    """把「工作区外写入」提升为独立的工作区行。

    会话 cwd 和实际干活的仓库经常不是同一个（实测某天 209 次写盘里 145 次在区外）。
    只统计不归类，表上就只剩一半工作量，日志正文也只能覆盖一半。
    这里按仓库身份拆成独立行，让「今天还动了哪些仓库」在表里可见；
    **归档侧仍然丢弃这些写入**，两者互不影响。

    不给伪行分 token：usage 是模型调用级的，没有目录维度，硬拆就是编数字。
    """
    for g in list(groups.values()):
        for key, files in g.get("extfiles", {}).items():
            row = groups.get("ext::" + key)
            if row is None:
                row = groups["ext::" + key] = {
                    "ws": key, "is_ext": True, "sessions": [], "prompts": [],
                    "files": {}, "tools": {}, "external": {}, "first": "", "last": "",
                    "turns": [], "tcls": {}, "ccmd": {},
                    "tok": {"in": 0, "out": 0, "cache_read": 0},
                    "extfiles": {}, "extspan": {},
                    "wcalls": 0, "whits": 0, "exts": 0}
            for rel, op in files.items():
                if row["files"].get(rel) != "write":
                    row["files"][rel] = op
            row["exts"] += (g.get("external") or {}).get(key, 0)
            sp = (g.get("extspan") or {}).get(key)
            if sp:
                if sp[0] and (not row["first"] or sp[0] < row["first"]):
                    row["first"] = sp[0]
                if sp[1] and (not row["last"] or sp[1] > row["last"]):
                    row["last"] = sp[1]


def _load_cols(sources=None, s_ms=None, e_ms=None):
    """解析范围内所有会话，返回 (stat, cols, adapters)。

    按范围统计时**只解析一次**，再对每个日期分别聚合——`collect_day` 连跑七天会让
    735MB 转录被解析七遍。这也是"预览与落盘各算一遍导致口径漂移"之外的另一个坑。
    """
    adapters = get_adapters(sources)
    idx = build_index(adapters)
    sids = sorted(idx.ids())

    stat = {"total": len(sids), "auto": 0, "no_transcript": 0, "out_of_range": 0,
            "no_meta": 0, "cand": 0, "parsed": 0, "hit": 0, "untimed": 0,
            "write_calls": 0, "write_hits": 0, "multi_day": 0, "failed": 0,
            "no_cwd": 0, "dup": 0, "meta_fixed": 0, "external": 0, "no_usage": 0}
    cols = []

    for sid in sids:
        info = idx.info(sid) or {}
        cwd = (info.get("cwd") or "").replace("\\", "/")
        if not cwd:
            stat["no_cwd"] += 1
            continue
        if info.get("meta_from") == "transcript":
            stat["meta_fixed"] += 1
        # 只排除后台自动化任务。is_playground 表示「在对话界面创建」，
        # 是所有正常会话的默认值，按它过滤会把日志清空。
        if info.get("automation"):
            stat["auto"] += 1
            continue
        path = idx.transcript(sid)
        if not path:
            stat["no_transcript"] += 1
            continue
        c_ms = to_ms(info.get("created"))
        u_ms = to_ms(info.get("updated")) or c_ms
        lo = c_ms if c_ms is not None else u_ms
        if lo is None:
            stat["no_meta"] += 1        # 无元数据：无从判断，保守解析
        elif s_ms is not None and not (u_ms >= s_ms and lo < e_ms):
            stat["out_of_range"] += 1
            continue
        stat["cand"] += 1
        ad = adapter_of(info.get("source") or "")
        col = DayCollector(cwd, sid, info.get("source") or "", info.get("title") or "")
        try:
            col.consume(ad.iter_events(path, cwd))
        except Exception as e:
            stat["failed"] += 1
            print(f"  [warn] {sid[:8]} 解析失败：{e}")
            continue
        stat["parsed"] += 1
        stat["untimed"] += col.untimed
        stat["dup"] += col.dup
        if len([d for d, b in col.days.items() if b["prompts"] or b["files"]]) > 1:
            stat["multi_day"] += 1
        if not any(sum(b["tok"].values()) for b in col.days.values()):
            stat["no_usage"] += 1
        cols.append(col)
    return stat, cols, adapters


def _empty_group(ws: str) -> dict:
    """一个工作区的累计桶。按天分组与跨天合并共用同一形状，才能无损相加。"""
    return {"ws": ws, "sessions": [], "prompts": [], "files": {}, "tools": {},
            "external": {}, "first": "", "last": "",
            "turns": [], "tcls": {}, "ccmd": {},
            "tok": {"in": 0, "out": 0, "cache_read": 0},
            "extfiles": {}, "extspan": {}, "wcalls": 0, "whits": 0, "exts": 0}


def _aggregate_day(cols, date_str: str):
    """把已解析的采集器按某一天归并成 {ws -> group}。返回 (groups, 命中会话数)。"""
    groups = {}
    hits = 0
    for col in cols:
        b = col.days.get(date_str)
        if not b or not (b["prompts"] or b["files"]):
            continue
        hits += 1
        g = groups.get(col.ws.lower())
        if g is None:
            g = groups[col.ws.lower()] = _empty_group(col.ws)
        g["sessions"].append({
            "sid": col.sid, "title": col.title, "source": col.source,
            "prompts": len(b["prompts"]), "files": len(b["files"]),
            "writes": sum(1 for v in b["files"].values() if v == "write"),
            "edits": sum(1 for v in b["files"].values() if v != "write"),
            "first": b["first"], "last": b["last"],
            "file_list": sorted(b["files"]),
        })
        g["prompts"].extend(b["prompts"])
        for rel, op in b["files"].items():
            if g["files"].get(rel) != "write":
                g["files"][rel] = op
        for n, c in b["tools"].items():
            g["tools"][n] = g["tools"].get(n, 0) + c
        for k, c in b["tcls"].items():
            g["tcls"][k] = g["tcls"].get(k, 0) + c
        for k, c in b["ccmd"].items():
            g["ccmd"][k] = g["ccmd"].get(k, 0) + c
        if b["turns"]:
            g["turns"].extend(b["turns"])
        for d, n in b["external"].items():
            g["external"][d] = g["external"].get(d, 0) + n
        for k, files in b["extfiles"].items():
            rec = g["extfiles"].setdefault(k, {})
            for rel, op in files.items():
                if rec.get(rel) != "write":
                    rec[rel] = op
        for k, sp in b["extspan"].items():
            cur = g["extspan"].setdefault(k, ["", ""])
            if sp[0] and (not cur[0] or sp[0] < cur[0]):
                cur[0] = sp[0]
            if sp[1] and (not cur[1] or sp[1] > cur[1]):
                cur[1] = sp[1]
        for k in ("in", "out", "cache_read"):
            g["tok"][k] += b["tok"][k]
        g["wcalls"] += b["wcalls"]
        g["whits"] += b["whits"]
        g["exts"] += b["exts"]
        for k in ("first", "last"):
            if b[k] and (not g[k] or (b[k] < g[k] if k == "first" else b[k] > g[k])):
                g[k] = b[k]
    return groups, hits


def _merge_group(dst: dict, src: dict):
    """把一天的分组合并进累计分组。两边形状必须一致（都来自 _empty_group）。"""
    dst["sessions"].extend(src["sessions"])
    dst["prompts"].extend(src["prompts"])
    dst["turns"].extend(src["turns"])
    for rel, op in src["files"].items():
        if dst["files"].get(rel) != "write":
            dst["files"][rel] = op
    for key in ("tools", "tcls", "ccmd", "external"):
        for k, c in src[key].items():
            dst[key][k] = dst[key].get(k, 0) + c
    for k, files in src["extfiles"].items():
        rec = dst["extfiles"].setdefault(k, {})
        for rel, op in files.items():
            if rec.get(rel) != "write":
                rec[rel] = op
    for k, sp in src["extspan"].items():
        cur = dst["extspan"].setdefault(k, ["", ""])
        if sp[0] and (not cur[0] or sp[0] < cur[0]):
            cur[0] = sp[0]
        if sp[1] and (not cur[1] or sp[1] > cur[1]):
            cur[1] = sp[1]
    for k in ("in", "out", "cache_read"):
        dst["tok"][k] += src["tok"][k]
    for k in ("wcalls", "whits", "exts"):
        dst[k] += src[k]
    if src["first"] and (not dst["first"] or src["first"] < dst["first"]):
        dst["first"] = src["first"]
    if src["last"] and (not dst["last"] or src["last"] > dst["last"]):
        dst["last"] = src["last"]


def tok_total(g: dict) -> int:
    """一组 token 的合计。**只算 input + output**。

    cache_read 是 input 的子集（实测 `total = input + output`），三者相加会把输入
    算成两倍——这个坑在 daylog 阶段踩过一次，规则写在这里，别再踩第二次。
    """
    tok = g.get("tok") or {}
    return int(tok.get("in", 0)) + int(tok.get("out", 0))


def _finalize_groups(groups: dict):
    """排序与派生字段。合并后必须再调一次——顺序变了，writes/edits 也要重算。"""
    for g in groups.values():
        g["prompts"].sort(key=lambda x: x[0])
        g["turns"].sort(key=lambda x: x["t"])
        g["writes"] = sum(1 for v in g["files"].values() if v == "write")
        g["edits"] = sum(1 for v in g["files"].values() if v != "write")


def collect_day(date_str: str, sources=None, merge_external: bool = False) -> dict:
    """把某一天的活动聚合成 {stat, groups, adapters}。

    预览（run_daylog）与落盘（cmd_log）共用同一份聚合，口径不会各走一路。
    纯函数式：只读转录，不写文件、不调模型。
    """
    try:
        s_ms, e_ms = day_range_ms(date_str)
    except Exception:
        return {"ok": False, "msg": f"日期格式应为 YYYY-MM-DD：{date_str}"}

    stat, cols, adapters = _load_cols(sources, s_ms, e_ms)
    groups, stat["hit"] = _aggregate_day(cols, date_str)
    if merge_external:
        _merge_external_groups(groups)
    _finalize_groups(groups)

    # 写盘口径统一到「当天」：会话级累计会把别的日子的写入算进来，与表格矛盾
    real = [g for g in groups.values() if not g.get("is_ext")]
    stat["write_calls"] = sum(g["wcalls"] for g in real)
    stat["write_hits"] = sum(g["whits"] for g in real)
    stat["external"] = sum(g["exts"] for g in real)

    return {"ok": True, "stat": stat, "groups": groups, "adapters": adapters}


def collect_range(days, sources=None, merge_external: bool = False) -> dict:
    """多天聚合：解析一次，按天归并，再合并成一份总数。

    `stats` 走这里，保证和 `daylog` 是同一套口径——同一个数字在两处对不上，
    用户就再也不会相信任何一个。
    """
    if not days:
        return {"ok": False, "msg": "日期范围为空"}
    try:
        s_ms, _ = day_range_ms(days[0])
        _, e_ms = day_range_ms(days[-1])
    except Exception:
        return {"ok": False, "msg": "日期格式应为 YYYY-MM-DD"}

    stat, cols, adapters = _load_cols(sources, s_ms, e_ms)
    total, daily = {}, []
    for d in days:
        groups, hits = _aggregate_day(cols, d)
        stat["hit"] += hits
        real = [g for g in groups.values() if not g.get("is_ext")]
        daily.append({
            "date": d, "hits": hits, "projects": len(real),
            "prompts": sum(len(g["prompts"]) for g in real),
            "files": sum(len(g["files"]) for g in real),
            "tokens": sum(tok_total(g) for g in real),
            "cache_read": sum(g["tok"]["cache_read"] for g in real),
        })
        for key, g in groups.items():
            dst = total.get(key)
            if dst is None:
                dst = total[key] = _empty_group(g["ws"])
            _merge_group(dst, g)
    if merge_external:
        _merge_external_groups(total)
    _finalize_groups(total)

    real = [g for g in total.values() if not g.get("is_ext")]
    stat["write_calls"] = sum(g["wcalls"] for g in real)
    stat["write_hits"] = sum(g["whits"] for g in real)
    stat["external"] = sum(g["exts"] for g in real)

    return {"ok": True, "stat": stat, "groups": total, "daily": daily,
            "adapters": adapters, "days": days}

def run_daylog(target: Path, date_str: str, sources=None, as_json=False,
               merge_external: bool = False) -> int:
    """打印某一天的开发活动分桶表。只读：不写文件、不调模型。"""
    r = collect_day(date_str, sources, merge_external)
    if not r["ok"]:
        print(r["msg"])
        return 2
    stat, groups, adapters = r["stat"], r["groups"], r["adapters"]

    if as_json:
        out = {
            "date": date_str,
            "sources": [a.name for a in adapters],
            "stat": stat,
            "workspaces": [{
                "workspace": g["ws"],
                # external_row=是否是「工作区外」伪行；external=区外写入计数（两个键不能同名，
                # 同名字典里后者覆盖前者，会把真工作区误标成区外行）
                "external_row": bool(g.get("is_ext")),
                "sessions": len(g["sessions"]),
                "prompts": len(g["prompts"]),
                "files": sorted(g["files"]),
                "tools": g["tools"],
                "external": g["external"],
                "tool_classes": g["tcls"],
                "cmd_classes": g["ccmd"],
                "tokens": g["tok"],
                "turns": [{
                    "time": t["t"], "prompt": t["prompt"],
                    "files": t["files"][:20],
                    "commands": [{"kind": k, "cmd": c} for k, c in t["cmds"]],
                    "errors": t["errors"],
                    "tokens": t["tok"],
                } for t in g["turns"]],
                "first": hhmm(g["first"]),
                "last": hhmm(g["last"]),
                "sessions_detail": g["sessions"],
            } for g in sorted(groups.values(), key=lambda x: -len(x["prompts"]))],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # ---------- 控制台预览
    print(f"=== daylog {date_str} · 零写入预览 ===\n")
    print(f"数据源        {', '.join(a.name for a in adapters)}")
    print(f"会话总数      {stat['total']}")
    print(f"  跳过·自动化   {stat['auto']}    （后台任务，不是人的工作）")
    print(f"  跳过·无转录   {stat['no_transcript']}")
    print(f"  跳过·不在当天 {stat['out_of_range']}    （时间窗与当天无交集）")
    print(f"  跳过·缺工作区 {stat['no_cwd']}    （拿不到 cwd，无法定位写入）")
    if stat["meta_fixed"]:
        print(f"  补元数据      {stat['meta_fixed']}    （库里没这行，已从转录头部补出 cwd/起始时间）")
    print(f"  实际解析      {stat['parsed']} / 候选 {stat['cand']}"
          + (f"，失败 {stat['failed']}" if stat["failed"] else ""))
    print(f"命中当天      {stat['hit']} 个会话，{len(groups)} 个工作区\n")

    if not groups:
        print("当天没有可归集的活动。可能原因：当天确实没开发；或会话时间戳缺失。")
        return 0

    W = 46
    head = (_pad("工作区", W) + _pad("会话", 6, True) + _pad("指令", 6, True)
            + _pad("写", 5, True) + _pad("改", 5, True) + _pad("工具", 7, True)
            + _pad("命令", 7, True) + "  跨度")
    print(head)
    print("-" * _dw(head))
    for g in sorted(groups.values(), key=lambda x: (-len(x["prompts"]), x["ws"])):
        ws = _cut(("↳ 区外 " if g.get("is_ext") else "") + g["ws"], W - 2)
        print(_pad(ws, W) + _pad(len(g["sessions"]), 6, True) + _pad(len(g["prompts"]), 6, True)
              + _pad(g["writes"], 5, True) + _pad(g["edits"], 5, True)
              + _pad(sum(g["tools"].values()), 7, True)
              + _pad(g["tcls"].get("shell", 0), 7, True)
              + f"  {hhmm(g['first'])}-{hhmm(g['last'])}")
    print()

    # 工具分类：表格里的「工具」是总数，这里回答「时间花在哪类动作上」
    cls_tot = {}
    for g in groups.values():
        for k, n in g["tcls"].items():
            cls_tot[k] = cls_tot.get(k, 0) + n
    if cls_tot:
        order = [("write", "写盘"), ("read", "读"), ("search", "检索"),
                 ("shell", "命令"), ("task", "任务"), ("generic", "其它"),
                 ("unknown", "未知")]
        seg = [f"{label} {cls_tot.get(k, 0)}" for k, label in order if cls_tot.get(k)]
        print("动作构成      " + " · ".join(seg))

    # 命令用途：能直接写进汇报的事实
    cc_tot = {}
    for g in groups.values():
        for k, n in g["ccmd"].items():
            cc_tot[k] = cc_tot.get(k, 0) + n
    if cc_tot:
        label = {"script": "脚本", "build": "构建", "test": "测试", "git": "git",
                 "deps": "装依赖", "inspect": "查看", "other": "其它"}
        seg = [f"{label.get(k, k)} {cc_tot[k]}"
               for k in ("build", "test", "deps", "git", "script", "inspect", "other")
               if cc_tot.get(k)]
        print("命令用途      " + " · ".join(seg))

    # token 用量：把「成本」落到天和轮次上。没有它，日志只能记「做了什么」，
    # 无法回答「这件事花了多少」——而后者才是管理者真正在算的账。
    tok_tot = {"in": 0, "out": 0, "cache_read": 0}
    for g in groups.values():
        for k in tok_tot:
            tok_tot[k] += g["tok"][k]
    if any(tok_tot.values()):
        seg = [f"输入 {_km(tok_tot['in'])}", f"输出 {_km(tok_tot['out'])}"]
        if tok_tot["cache_read"]:
            # total_tokens = input + output 已成立，说明 cache_read 是 input 的子集；
            # 它只能算「命中率」，不能与 input 相加，否则输入量会被重复计一遍。
            seg.append(f"其中缓存命中 {_km(tok_tot['cache_read'])}"
                       f"（{tok_tot['cache_read'] * 100 // max(1, tok_tot['in'])}%）")
        print("token 用量    " + " · ".join(seg))
    print()

    print("数据完整性")
    print(f"  · 无时间戳事件      {stat['untimed']}"
          + ("   ← 无法归日，已丢弃" if stat["untimed"] else "   （事件都带 ts，时间粒度可用）"))
    lost = stat["write_calls"] - stat["write_hits"] - stat["external"]
    print(f"  · 写盘调用          {stat['write_calls']} 次")
    print(f"      工作区内         {stat['write_hits']}")
    print(f"      工作区外         {stat['external']}   ← 未计入上表，别处的写入")
    if lost:
        print(f"      无路径字段       {lost}   ← 工具参数里没有文件路径，无法定位")
    ext_dirs = {}
    for g in groups.values():
        for d, n in g["external"].items():
            ext_dirs[d] = ext_dirs.get(d, 0) + n
    if ext_dirs:
        top = sorted(ext_dirs.items(), key=lambda x: -x[1])[:3]
        print(f"      主要去向         " + "、".join(f"{_cut(d, 20)}({n})" for d, n in top))
        if stat["external"] > stat["write_hits"]:
            print("      ⚠ 外部写入多于工作区内写入：该会话的实际干活目录和工作区不是同一个，")
            print("        只看表格会严重低估当天的工作量，日志正文必须把外部目录算进去。")
    print(f"  · 重复指令已压重     {stat['dup']}"
          + ("   ← 上下文压缩导致的原文重放" if stat["dup"] else ""))
    print(f"  · 跨天会话          {stat['multi_day']}"
          + ("   ← 同一会话会出现在多天，按天切分属预期" if stat["multi_day"] else ""))
    print(f"  · 无 token 记录的会话 {stat['no_usage']} / 已解析 {stat['parsed']}"
          + ("   ← 该平台不写 usage，成本侧只能留空，不可用 0 顶替"
             if stat["no_usage"] else "   （成本侧数据完整）"))
    print()

    print("明细（默认不脱敏；落盘稿会过 desensitize）")
    shown = 0
    for g in sorted(groups.values(), key=lambda x: -len(x["prompts"])):
        if shown >= 6:
            print(f"  … 另有 {len(groups) - shown} 个工作区未展示")
            break
        shown += 1
        if g.get("is_ext"):
            print(f"\n[区外] {_cut(g['ws'], 56)}  不是会话工作区，"
                  f"但当天有 {g['exts']} 次写入落在这里")
            fl = [x for x in sorted(g["files"]) if not SENSITIVE_PATH_HINT.search(x)]
            hidden = len(g["files"]) - len(fl)
            print(f"  文件：{'、'.join(_cut(x, 26) for x in fl[:8])}"
                  + (f" 等 {len(fl)} 个" if len(fl) > 8 else "")
                  + (f"（另 {hidden} 个敏感路径未列出）" if hidden else ""))
            if g["first"]:
                print(f"  时段：{hhmm(g['first'])}-{hhmm(g['last'])}")
            continue
        print(f"\n[{_cut(g['ws'], 64)}]  指令 {len(g['prompts'])} · 文件 {len(g['files'])}")
        # 只列「有产出」的轮次：改了不少文件、跑了命令、或撞了错误的那几条，
        # 这些才是日志的候选句子；纯问答轮次不进正文。
        rich = [t for t in g["turns"]
                if t["files"] or t["cmds"] or t["errors"]]
        # 先按产出量取前 10 条，再按时间排回顺序：日志是叙事，时间乱序读不下去，
        # 但纯粹的「最早 10 条」会把重活漏掉。
        rich.sort(key=lambda t: -(len(t["files"]) + len(t["cmds"]) + t["errors"]))
        rich = sorted(rich[:10], key=lambda t: t["t"])
        for t in rich[:8]:
            bits = []
            if t["files"]:
                bits.append(f"{len(t['files'])} 文件")
            if t["cmds"]:
                kinds = {}
                for k, _ in t["cmds"]:
                    kinds[k] = kinds.get(k, 0) + 1
                lab = {"build": "构建", "test": "测试", "git": "git",
                       "deps": "依赖", "script": "脚本", "inspect": "查看",
                       "other": "命令"}
                bits.append(" ".join(f"{lab.get(k, k)}×{n}" for k, n in kinds.items()))
            if t["errors"]:
                bits.append(f"错误 {t['errors']}")
            tk = (t.get("tok") or {}).get("out", 0) + (t.get("tok") or {}).get("in", 0)
            if tk:
                bits.append(f"{_km(tk)} tok")
            print(f"  {t['t']}  {_cut(t['prompt'], 52)}")
            print(f"           → {' · '.join(bits)}")
            if t["files"]:
                print(f"             {'、'.join(_cut(x, 26) for x in t['files'][:5])}"
                      + (" …" if len(t["files"]) > 5 else ""))
        if not rich:
            for t, txt in g["prompts"][:6]:
                print(f"  {t}  {_cut(txt, 60)}")
        elif len(rich) < len(g["turns"]):
            print(f"         … 另 {len(g['turns']) - len(rich)} 条纯问答轮次未展示")
        if g["external"]:
            ex = sorted(g["external"].items(), key=lambda x: -x[1])[:4]
            print(f"  区外：{'、'.join(f'{_cut(d, 20)}({n})' for d, n in ex)}"
                  f"  共 {g['exts']} 次写入，不在本工作区内")
    return 0


def render_daylog_md(date_str: str, r: dict, note: str = "") -> str:
    """把一天的分桶结果渲染成 Markdown 清点稿。

    刻意只写事实、不做判断：这里没有「本周完成/进行中/阻塞」的归纳，因为那需要
    读懂上下文，属于模型该做的事。清点稿的职责是把证据摆齐——每条指令后面直接
    跟它改了哪些文件、跑了什么命令、花了多少 token，让归纳有据可依。

    纯函数：同样的入参永远得到同样的字符串，落盘才能幂等（定时任务每天跑不会
    产生无意义的 diff）。
    """
    stat, groups = r["stat"], r["groups"]
    real = [g for g in groups.values() if not g.get("is_ext")]
    ext = [g for g in groups.values() if g.get("is_ext")]
    real.sort(key=lambda x: -len(x["prompts"]))

    tok = {"in": 0, "out": 0, "cache_read": 0}
    for g in real:
        for k in tok:
            tok[k] += g["tok"][k]

    L = [f"# 工作日志 {date_str}", ""]
    if note:
        L += [f"> {note}", ""]
    L += [f"> 由 dev-project-archivist 从本地 AI 会话自动清点，**未调用模型**。",
          f"> 数据源：{'、'.join(a.name for a in r['adapters'])} · "
          f"会话 {stat['hit']} 个 · 工作区 {len(real)} 个", ""]

    total_prompts = sum(len(g["prompts"]) for g in real)
    total_files = sum(len(g["files"]) for g in real)
    if not real:
        L += ["当天没有可归集的活动。", ""]
    else:
        L += ["## 一、按项目清点", ""]
        for i, g in enumerate(real, 1):
            span = f"{hhmm(g['first'])}–{hhmm(g['last'])}" if g["first"] else "时间未知"
            L += [f"### {i}. {desensitize(g['ws'])}", "",
                  f"- 时段 {span} · 指令 {len(g['prompts'])} 条 · "
                  f"文件 {len(g['files'])} 个（写 {g['writes']} / 改 {g['edits']}）"]
            if g["tok"]["in"] or g["tok"]["out"]:
                L.append(f"- 用量 输入 {_km(g['tok']['in'])} · 输出 {_km(g['tok']['out'])}")
            if g["external"]:
                top = sorted(g["external"].items(), key=lambda x: -x[1])[:3]
                L.append(f"- 另有 {g['exts']} 次写入落在工作区外："
                         + "、".join(f"{safe_path_label(k)}({n})" for k, n in top))
            L.append("")
            # 只列有产出的轮次：纯问答轮次进正文只会把日志冲成聊天记录
            rich = [x for x in g["turns"] if x["files"] or x["cmds"] or x["errors"]]
            rich.sort(key=lambda x: -(len(x["files"]) + len(x["cmds"]) + x["errors"]))
            rich = sorted(rich[:20], key=lambda x: x["t"])
            for x in rich:
                bits = []
                if x["cmds"]:
                    kinds = {}
                    for k, _ in x["cmds"]:
                        kinds[k] = kinds.get(k, 0) + 1
                    lab = {"script": "脚本", "build": "构建", "test": "测试",
                           "deps": "依赖", "git": "git", "inspect": "查看", "other": "命令"}
                    bits.append(" ".join(f"{lab.get(k, k)}×{n}" for k, n in kinds.items()))
                if x["errors"]:
                    bits.append(f"报错 {x['errors']}")
                used = x["tok"]["in"] + x["tok"]["out"]   # cache_read 是 in 的子集，不能加
                tail = f"（{_km(used)} tok）" if used else ""
                L.append(f"- `{x['t']}` {desensitize(_cut(x['prompt'], 120))}"
                         + (f" — {' · '.join(bits)}" if bits else "") + tail)
                for fp in x["files"][:6]:
                    L.append(f"  - `{desensitize(fp)}`")
                if len(x["files"]) > 6:
                    L.append(f"  - …另 {len(x['files']) - 6} 个文件")
            if not rich:
                L.append("- （当天只有问答，没有落到文件或命令上）")
            L.append("")
        L += ["## 二、当天合计", "",
              f"- 指令 **{total_prompts}** 条 · 涉及文件 **{total_files}** 个 · "
              f"工作区 **{len(real)}** 个"]
        if any(tok.values()):
            L.append(f"- token 输入 {_km(tok['in'])} · 输出 {_km(tok['out'])}"
                     + (f" · 其中缓存命中 {_km(tok['cache_read'])}"
                        f"（{tok['cache_read'] * 100 // max(1, tok['in'])}%）"
                        if tok["cache_read"] else ""))
        cls = {}
        cmd = {}
        for g in real:
            for k, n in g["tcls"].items():
                cls[k] = cls.get(k, 0) + n
            for k, n in g["ccmd"].items():
                cmd[k] = cmd.get(k, 0) + n
        if cls:
            order = [("write", "写盘"), ("read", "读"), ("search", "检索"),
                     ("shell", "命令"), ("task", "任务"), ("generic", "其它")]
            L.append("- 动作构成：" + " · ".join(f"{lb} {cls[k]}" for k, lb in order if cls.get(k)))
        if cmd:
            lab = {"script": "脚本", "build": "构建", "test": "测试", "deps": "装依赖",
                   "git": "git", "inspect": "查看", "other": "其它"}
            L.append("- 命令用途：" + " · ".join(
                f"{lab.get(k, k)} {cmd[k]}"
                for k in ("build", "test", "deps", "git", "script", "inspect", "other")
                if cmd.get(k)))
        L.append("")

    if ext:
        L += ["## 三、工作区外的改动", "",
              "> 这些写入不在任何会话的工作区里（会话 cwd 与实际干活目录不一致）。",
              "> 归档侧按设计丢弃，日志侧必须列出——否则当天工作量会被低估。", ""]
        for g in sorted(ext, key=lambda x: -x["exts"]):
            L.append(f"- **{safe_path_label(desensitize(g['ws']))}** {g['exts']} 次写入"
                     f"（{hhmm(g['first'])}–{hhmm(g['last'])}）")
            shown = [x for x in sorted(g["files"]) if not SENSITIVE_PATH_HINT.search(x)]
            hidden = len(g["files"]) - len(shown)
            if shown:
                L.append("  " + "、".join(f"`{desensitize(x)}`" for x in shown[:8])
                         + (f" 等 {len(shown)} 个" if len(shown) > 8 else ""))
            if hidden:
                L.append(f"  另有 {hidden} 个敏感路径（凭据/密钥类）未列出")
        L.append("")

    warn = []
    if stat["untimed"]:
        warn.append(f"无时间戳事件 {stat['untimed']} 条，未归入任何一天")
    if stat["failed"]:
        warn.append(f"{stat['failed']} 个会话解析失败，内容缺失")
    if stat["no_usage"]:
        warn.append(f"{stat['no_usage']} 个会话没有 token 记录，用量只能留空")
    lost = stat["write_calls"] - stat["write_hits"] - stat["external"]
    if lost > 0:
        warn.append(f"{lost} 次写盘调用没有文件路径，无法定位")
    if warn:
        L += ["## 四、数据缺口", ""] + [f"- {w}" for w in warn] + [""]

    L += ["---", "",
          f"生成方式：`archiver.py log --date {date_str}`（只读转录，确定性输出）", ""]
    return "\n".join(L)


def cmd_log(target: Path, date_str: str, sources=None, out_dir: str = "",
            merge_external: bool = True) -> int:
    """把某天的清点稿写到 logs/YYYY-MM-DD.md。

    默认开启 merge_external：落盘稿是给人看的，区外改动不列出来就是漏报。
    """
    r = collect_day(date_str, sources, merge_external)
    if not r["ok"]:
        print(r["msg"])
        return 2
    d = Path(out_dir) if out_dir else (target / "logs")
    path = d / f"{date_str}.md"
    md = render_daylog_md(date_str, r)
    old = read_text(path) if path.exists() else ""
    if old == md:
        print(f"无变化：{path}")
        return 0
    atomic_write(path, md)
    real = [g for g in r["groups"].values() if not g.get("is_ext")]
    print(f"已写入 {path}")
    print(f"  {date_str}：工作区 {len(real)} 个 · "
          f"指令 {sum(len(g['prompts']) for g in real)} 条 · "
          f"文件 {sum(len(g['files']) for g in real)} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 对话项目归档器")
    ap.add_argument("--version", action="version",
                    version=f"dev-project-archivist {__version__}")
    ap.add_argument("cmd",
                    choices=["scan", "render", "run", "pending", "status",
                             "git-clean", "daylog", "log", "find", "stats", "report",
                             "restore", "sync", "manage"],
                    help="scan=采集/render=渲染/run=全流程/pending=待归纳/status=状态/"
                         "git-clean=重建归档器创建的 git 仓库以清除历史大对象/"
                         "daylog=按天汇总开发活动（预览，零写入）/"
                         "log=把某天写成清点稿 <target>/logs/YYYY-MM-DD.md/"
                         "find=跨项目检索（名字/文件名/指令原文，只读）/"
                         "stats=跨天账本：最近 N 天的对话/文件/token（只读）/"
                         "report=写成单文件 HTML 账本（可截图分享）/"
                         "restore=按日恢复归档快照到副本目录（默认只预览）/"
                         "sync=归档仓库同步：看哪些没推，或 --push 推到各自远端/"
                         "manage=本地浏览器管理台（--port 可改端口，仅监听 127.0.0.1）")
    ap.add_argument("query", nargs="?", default="",
                    help="find 的检索词，可空格分隔多个词（全部命中才计入）")
    ap.add_argument("--open", action="store_true", dest="open_it",
                    help="find 命中后打开第一条的原对话；report 生成后用浏览器打开")
    ap.add_argument("--target", default=str(DEFAULT_TARGET), help="归档输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印分类结果，不写任何文件")
    ap.add_argument("--limit", type=int, default=0, help="只处理最近 N 个项目")
    ap.add_argument("--only", default="", help="只处理名称/路径含该关键字的项目")
    ap.add_argument("--force", action="store_true", help="忽略指纹，强制重扫")
    ap.add_argument("--full-copy", action="store_true", help="复制全部文件（含媒体/二进制）")
    ap.add_argument("--min-code", type=int, default=None, help="判定代码项目的源码文件数下限")
    ap.add_argument("--no-rename", action="store_true", help="渲染时不按摘要标题重命名目录")
    ap.add_argument("--source", default="all",
                    help="数据来源：all（默认，自动探测本机全部平台）或逗号分隔的适配器名")
    ap.add_argument("--date", default="", help="daylog 的目标日期 / stats 的截止日期 / "
                                              "restore 的恢复日期 YYYY-MM-DD；"
                                              "restore 不传则列出可恢复的日期")
    ap.add_argument("--all", action="store_true", dest="show_all",
                    help="scan --dry-run 时连空壳会话一起列出（默认折叠）")
    ap.add_argument("--explain", nargs="?", const="", default=None,
                    help="打印判定依据：--explain [关键字]（只读，不写任何文件）")
    ap.add_argument("--json", action="store_true", help="daylog/stats/find 以 JSON 输出（供脚本消费）")
    ap.add_argument("--days", type=int, default=0,
                    help="回溯天数：stats 默认 7，report 默认 30")
    ap.add_argument("--merge-external", action="store_true",
                    help="daylog 把工作区外的写入按仓库提升为独立行（默认只在体检里计数）")
    ap.add_argument("--no-merge-external", action="store_true",
                    help="log 落盘时不并列工作区外的改动（默认会列，漏报风险更大）")
    ap.add_argument("--out", default="",
                    help="log 的输出目录（默认 <target>/logs）/ report 的输出目录（默认 <target>/report）"
                         "/ restore 的输出目录（默认 <target>/_restore）")
    ap.add_argument("--write", action="store_true", dest="write_it",
                    help="restore 真正展开副本（默认只预览，不写任何文件）")
    ap.add_argument("--push", action="store_true",
                    help="sync 真正提交并推送到各归档仓库自己配的远端（默认只预览；"
                         "未配远端的不推，永不 force）")
    ap.add_argument("--port", type=int, default=8765,
                    help="manage 管理台监听端口（默认 8765；0 表示自动选空闲端口）")
    ap.add_argument("--no-browser", action="store_true",
                    help="manage 启动时不自动打开浏览器")
    a = ap.parse_args()

    sources = [x.strip() for x in (a.source or "").split(",") if x.strip()]

    target = Path(a.target)
    if a.cmd == "manage":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from manage_ui import main as manage_main
        argv = ["--target", str(target), "--port", str(a.port)]
        if a.no_browser:
            argv.append("--no-browser")
        old_argv = sys.argv
        sys.argv = [sys.argv[0]] + argv
        try:
            return manage_main()
        finally:
            sys.argv = old_argv
    if a.cmd == "daylog":
        # 只读命令：不需要目标目录，也不加锁
        return run_daylog(target, a.date or datetime.now().strftime("%Y-%m-%d"),
                          sources=sources, as_json=a.json,
                          merge_external=a.merge_external)
    if a.cmd == "log":
        return cmd_log(target, a.date or datetime.now().strftime("%Y-%m-%d"),
                       sources=sources, out_dir=a.out,
                       merge_external=not a.no_merge_external)
    if a.cmd == "stats":
        # 只读命令：不需要目标目录，也不加锁
        return cmd_stats(sources, days=a.days or 7, end=a.date, as_json=a.json)
    if a.cmd == "report":
        # 只写一个 html 文件，不碰归档正文，因此不加锁
        return cmd_report(target, sources=sources, days=a.days or 30,
                          end=a.date, out=a.out, open_it=a.open_it)
    if a.cmd == "find":
        # 只读命令：不加锁、不写任何文件
        return cmd_find(target, a.query, as_json=a.json,
                        show_all=a.show_all, open_it=a.open_it)
    if a.cmd == "restore":
        # 只读归档仓库，写入点只有 <target>/_restore/：两边不共享文件，
        # 因此不加锁（加了反而会让恢复在定时任务跑的时候白等一小时）
        return cmd_restore(target, a.date, only=a.only, out=a.out,
                           write=a.write_it, force=a.force, as_json=a.json)
    if a.cmd == "status":
        return cmd_status(target)
    if a.cmd == "sync":
        # 只动各归档仓库自己（提交 + 推送），不碰归档正文与 state.json，
        # 因此不加锁：定时任务正在写的时候也该能查「推到哪儿了」。
        return cmd_sync(target, push=a.push, only=a.only, as_json=a.json)
    if a.cmd == "pending":
        return cmd_pending(target)
    if a.cmd == "git-clean":
        return cmd_git_clean(target)

    if a.explain is not None:
        return run_scan(target, dry_run=True, limit=a.limit, only=a.only,
                        force=True, min_code=a.min_code, sources=sources,
                        show_all=True, explain=a.explain)

    if a.dry_run:
        return run_scan(target, dry_run=True, limit=a.limit, only=a.only,
                        force=True, min_code=a.min_code, sources=sources,
                        show_all=a.show_all)

    target.mkdir(parents=True, exist_ok=True)
    with Lock(target):
        if a.cmd in ("scan", "run"):
            res = run_scan(target, limit=a.limit, only=a.only, force=a.force,
                           full_copy=a.full_copy, min_code=a.min_code, sources=sources)
            if isinstance(res, int) and res != 0:
                return res
            if a.cmd == "run" and isinstance(res, dict):
                parts = []
                if res["added"]:
                    parts.append(f"新增 {len(res['added'])}：" + "、".join(res["added"][:6]))
                if res["updated"]:
                    parts.append(f"更新 {len(res['updated'])}：" + "、".join(res["updated"][:6]))
                if res["removed"]:
                    parts.append(f"失联 {len(res['removed'])}：" + "、".join(res["removed"][:6]))
                if res.get("pending"):
                    parts.append(f"待归纳 {len(res['pending'])} 个")
                append_changelog(target, "扫描", "；".join(parts) or "无变化")
        if a.cmd in ("render", "run"):
            rc = run_render(target, rename=not a.no_rename)
            if rc != 0:
                return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
