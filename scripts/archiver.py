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
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# 适配器包与主程序同目录；脚本直跑时 sys.path[0] 已是本目录，
# 但从其他位置 import 时需显式补一次，保证两种方式都能加载。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import REGISTRY, build_index, get_adapter, get_adapters  # noqa: E402
from adapters.base import (EV_ASSISTANT, EV_PROMPT,  # noqa: E402
                           EV_TITLE, EV_TOOL, EV_TOOL_RESULT, EV_TS, EV_WRITE)

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
    t = re.sub(r"^[@\s#]+", "", (t or "").strip())
    t = re.split(r"[，。；：,;:！？!?\n]", t)[0].strip()
    return t[:maxlen] if len(t) > maxlen else t


JUNK_TITLE_PAT = re.compile(
    r"^(e|d|c|a|b|summary|image#?\d+|screenshot\d*|clipboard[_-]?\d*|"
    r"untitled|new chat|新对话|\d+)$", re.I)


def is_junk_title(t: str) -> bool:
    """识别明显无意义的标题（单字母、图片占位名等）"""
    t = (t or "").strip()
    if len(t) < 4:
        return True
    return bool(JUNK_TITLE_PAT.match(t))


def pick_title(titles: list) -> str:
    """从若干候选标题中挑最像『项目名』的一个（短、不含请求语气）"""
    cands = [t for t in titles if t and t.strip() and not is_junk_title(t)]
    if not cands:
        return ""

    def score(t):
        s = 0.0
        if len(t) <= 24:
            s += 2
        if not re.search(r"(请|帮我|需要|我们的|要求|下面|现在)", t):
            s += 2
        if re.search(r"(系统|工具|平台|应用|插件|归档|助手|App|app)", t):
            s += 1
        return s - len(t) / 100.0

    return short_title(max(cands[:6], key=score))


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
    s = SECRET_KV_PAT.sub(lambda m: re.split(r"[:=]", m.group(0), 1)[0] + "=<REDACTED>", s)
    return s


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
        """预命名：优先取最像项目名的会话标题，其次取语言/目录名"""
        titles = [s.get("title") for s in sorted(self.sessions,
                                                 key=lambda x: x.get("created") or 0)]
        t = pick_title([x for x in titles if x])
        if t:
            return safe_name(t)
        if self.cls.get("langs"):
            return safe_name(f"{self.cls['langs'][0][0]} 项目")
        return safe_name(Path(self.workspace).name)

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
    """会话标题无效（如 'E' / 'summary'）时，用首条真实提问重新预命名"""
    if p.name and not is_junk_title(p.name):
        return
    for s in p.sessions:
        pr = parses.get(s["sid"]) or {}
        for x in (pr.get("prompts") or [])[:2]:
            t = short_title(x, 20)
            if len(t) >= 4:
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


def git_init(adir: Path, name: str) -> str:
    """为项目目录初始化独立仓库（幂等）。返回 ok / exists / fail"""
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
            subprocess.run(["git", "add", "-A"], cwd=str(adir), capture_output=True,
                           timeout=120)
            subprocess.run(["git", "-c", "user.name=archiver",
                            "-c", "user.email=archiver@local",
                            "commit", "-q", "-m", f"sync: 同步 {name} 归档内容"],
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
                        "commit", "-q", "-m", f"init: 归档 {name}"],
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
def run_scan(target: Path, dry_run=False, limit=0, only="", force=False,
             full_copy=False, min_code=None, verbose=True, sources=None):
    t0 = now_ts()
    min_code = min_code or DEFAULTS["min_code_files"]
    adapters = get_adapters(sources)
    by_name = {a.name: a for a in adapters}
    idx = build_index(adapters)
    all_ids = idx.ids()
    n_transcript = sum(1 for sid in all_ids if idx.transcript(sid))
    log(f"[1/5] 会话索引：{len(all_ids)} 个会话，{n_transcript} 份转录")

    # 供 --only 同时匹配归档目录名（state 尚未加载，先轻量读一次）
    _dir_map = {}
    try:
        _st = json.loads(read_text(target / STATE_FILE) or "{}")
        _dir_map = {k: (v.get("dir") or "") for k, v in (_st.get("projects") or {}).items()}
    except Exception:
        pass
    projects = collect_projects(idx, min_code, only=only, limit=limit, dir_map=_dir_map)
    log(f"[2/5] 归并完成：{len(projects)} 个项目单元")

    if dry_run:
        print()
        print(f"{'日期':<11} {'类型':<7} {'语言':<14} {'码':>4} {'档':>4} {'文件':>5} 项目")
        print("-" * 92)
        for p in projects:
            print(f"{day(p.first_ts):<11} {p.type:<7} {(p.cls.get('language') or '—')[:13]:<14} "
                  f"{p.cls['code']:>4} {p.cls['doc']:>4} {p.cls['total_files']:>5} "
                  f"{(p.name + '  ' + Path(p.workspace).name)[:34]}")
        print("-" * 92)
        code_n = sum(1 for p in projects if p.type == "代码项目")
        log(f"小计：代码项目 {code_n} / 文档任务 "
            f"{sum(1 for p in projects if p.type == '文档任务')} / 其余 "
            f"{len(projects) - code_n - sum(1 for p in projects if p.type == '文档任务')}")
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
        make_open_bat(adir, latest["sid"] if latest else "",
                      (latest.get("source") or "") if latest else "")

        # 独立 git 仓库：便于日后提交与他人 clone 继续开发
        # 注意：git 状态不写入 manifest（否则每次提交都会产生 manifest 变更）
        if DEFAULTS["git_enabled"]:
            git_init(adir, p.name)

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
    log(f"[5/5] 扫描完成：更新 {len(touched)} / 跳过 {skipped} / 待归纳 {len(pending)}，"
        f"耗时 {now_ts() - t0:.1f}s")
    return {"added": added, "updated": updated, "removed": newly_missing,
            "pending": pending, "skipped": skipped}


# ================================================================ 渲染阶段
CONVERSATIONS_FILE = "conversations.md"
OPEN_BAT = "打开原对话.bat"


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
    L.append(f"> 跳转方式：{open_hint_of(src)}，或双击项目根目录的 `{OPEN_BAT}`。")
    L.append("")
    return "\n".join(L)


def make_open_bat(adir: Path, latest_sid: str, source: str = "") -> bool:
    """生成双击即可跳回最近一次原对话的快捷方式。

    有深链的平台生成 .bat 直接拉起（内容纯 ASCII，避免编码问题）；
    无深链的平台改生成同名 .md 说明，避免产出点不开的死链。
    """
    if not latest_sid:
        return False
    link = deep_link_of(latest_sid, source)
    if link:
        body = ("@echo off\r\n"
                "start \"\" \"%s\"\r\n" % link)
        try:
            atomic_write(adir / OPEN_BAT, body)
            return True
        except Exception:
            return False
    ad = adapter_of(source)
    body = ("# 打开原始对话\n\n"
            "本项目由 **%s** 中的会话逐步开发而成，该平台不支持一键跳转。\n\n"
            "- 会话 ID：`%s`\n"
            "- 打开方式：%s\n" % (ad.display or ad.name, latest_sid, ad.open_hint()))
    try:
        atomic_write(adir / OPEN_BAT.rsplit(".", 1)[0] + ".md", body)
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
        L.append(f"> 也可双击项目根目录的 `{OPEN_BAT}` 直达最近一次会话。")
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
        s = load_summary(adir)
        if rename and s.get("title") and st.get("name") != s["title"]:
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
    if DEFAULTS["git_enabled"]:
        for st, adir in rows:
            if st.get("dir") and (adir / ".git").exists():
                git_init(adir, st.get("name") or adir.name)

    log(f"[render] README 更新 {changed} 个，索引项目 {len(rows)} 个")
    return 0


def write_index(target: Path, rows: list, state: State, ov: dict = None):
    now = iso(now_ts())
    cache = {}

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
        "projects": [{
            "name": nm(st, adir),
            "dir": st.get("dir"), "type": st.get("type"),
            "language": effective_summary(adir, cache).get("language") or st.get("language"),
            "first_dev": day(st.get("first_ts")), "last_dev": day(st.get("last_ts")),
            "archived_at": day(st.get("last_archived")),
            "sessions": st.get("sessions", 0), "files": st.get("files", 0),
            "status": st.get("status"), "workspace": st.get("workspace"),
            "purpose": (pick_override(ov or {}, st.get("workspace"), adir.name).get("purpose")
                        or effective_summary(adir, cache).get("purpose") or "")[:200],
        } for st, adir in rows],
    }
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
    return 0


def cmd_git_clean(target: Path):
    """重建由归档器创建的 git 仓库，清掉历史提交里的大对象。

    只在所有提交信息均由归档器产生（init: 归档 / sync: 同步）时才重建，
    避免破坏用户自己的提交历史。
    """
    import subprocess
    state = State(target)
    done = skip = 0
    for k, st in state.data["projects"].items():
        if not st.get("archived") or not st.get("dir"):
            continue
        adir = target / st["dir"]
        gitdir = adir / ".git"
        if not adir.exists() or not gitdir.exists():
            continue
        try:
            r = subprocess.run(["git", "log", "--format=%s"], cwd=str(adir),
                               capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
            msgs = [m for m in (r.stdout or "").splitlines() if m.strip()]
        except Exception:
            msgs = []
        if msgs and not all(m.startswith(("init: 归档", "sync: 同步")) for m in msgs):
            print(f"  跳过（含人工提交）：{st['dir']}")
            skip += 1
            continue
        try:
            shutil.rmtree(gitdir)
        except Exception as e:
            print(f"  失败：{st['dir']} {e}")
            skip += 1
            continue
        git_init(adir, st.get("name") or st["dir"])
        print(f"  重建：{st['dir']}")
        done += 1
    print(f"git 重建完成：{done} 个，跳过 {skip} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 对话项目归档器")
    ap.add_argument("cmd", choices=["scan", "render", "run", "pending", "status", "git-clean"],
                    help="scan=采集/render=渲染/run=全流程/pending=待归纳/status=状态/"
                         "git-clean=重建归档器创建的 git 仓库以清除历史大对象")
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
    a = ap.parse_args()

    sources = [x.strip() for x in (a.source or "").split(",") if x.strip()]

    target = Path(a.target)
    if a.cmd == "status":
        return cmd_status(target)
    if a.cmd == "pending":
        return cmd_pending(target)
    if a.cmd == "git-clean":
        return cmd_git_clean(target)

    if a.dry_run:
        return run_scan(target, dry_run=True, limit=a.limit, only=a.only,
                        force=True, min_code=a.min_code, sources=sources)

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
