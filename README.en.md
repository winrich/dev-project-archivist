# 🗂️ Conversation Project Archivist · Turn AI chat software into a source archive you can keep building

[English](README.en.md) · [简体中文](README.md)

**Read local sessions → identify projects → archive clean source → jump back to the conversation.**

> Software built through chat becomes unfindable within three months. This tool goes and gets it back.

<p align="center">
  <a href="https://github.com/winrich/dev-project-archivist/releases"><img src="https://img.shields.io/github/v/release/winrich/dev-project-archivist?color=blueviolet" alt="Release"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/github/license/winrich/dev-project-archivist" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.8%2B-blue.svg" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen.svg" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/WorkBuddy-supported-blueviolet.svg" alt="WorkBuddy">
  <img src="https://img.shields.io/badge/Claude_Code-supported-blueviolet.svg" alt="Claude Code">
</p>

<p align="center">
  <a href="https://github.com/winrich/dev-project-archivist" target="_blank">
    <img src="https://img.shields.io/github/stars/winrich/dev-project-archivist?style=social" alt="GitHub stars">
  </a>
  <a href="https://github.com/winrich/dev-project-archivist/commits/main" target="_blank">
    <img src="https://img.shields.io/github/last-commit/winrich/dev-project-archivist" alt="Last commit">
  </a>
  <img src="https://img.shields.io/github/repo-size/winrich/dev-project-archivist" alt="Repo size">
</p>

<table align="center">
  <tr>
    <td align="center"><b>📦 27 projects</b><br/><sub>archived on one real machine</sub></td>
    <td align="center"><b>📉 91 → 49 MB</b><br/><sub>after keeping source only</sub></td>
    <td align="center"><b>⚡ 0 dependencies</b><br/><sub>pure Python standard library</sub></td>
    <td align="center"><b>🔗 Back to the chat</b><br/><sub>pick up where you left off</sub></td>
  </tr>
</table>

> ⭐ **If this tool recovered a few projects you thought you had lost, a star is appreciated.**

---

## 🤔 Does this sound familiar

AI made writing software easy, but **the output is scattered across conversations**, which created a new set of problems:

- 😫 A small tool you built six months ago — you cannot even find which window holds the source
- 😫 Projects spread over dozens of sessions; copying them by hand never ends
- 😫 You want to change something, but cannot recall why it was designed this way
- 😫 A full backup would work, except `node_modules`, screenshots and build output are 90% of it
- 😫 You finally collected the source, then it breaks on another machine or in someone else's hands
- 😫 The archive keeps growing and a fresh `git clone` takes minutes

**This tool turns that into a pipeline**: scan sessions → classify projects → extract source → render docs → sync incrementally. Open `INDEX.md` when it finishes, and every project is a repository you can `git clone` and keep developing.

---

## 🗺️ How it differs from the alternatives

| Approach | What you get | What it costs |
|---|---|---|
| Copy and paste by hand | Only the projects you remember | Slow, and whatever you missed stays lost |
| Back up whole directories | Source plus dependencies, screenshots, build output | Tens of GB, uncloneable, untransferable |
| Feed every session to an LLM | A nicely written description | No source code, high cost, privacy gamble |
| **This tool** | Clean source + background + per-module features + implementation notes + links back to the chat | One command, zero dependencies |

---

## 🎯 First, decide: do you need this tool, or a specific project?

| Your goal | What to do |
|---|---|
| I use AI coding agents too, and want to collect **my own** conversation projects | Install this tool (keep reading) |
| I just want to take over someone's finished project and keep developing it | **Do not install.** Take that project folder (with `source/`, `.git/`, `README.md`) and `git clone` or unzip it |
| I want to hand one of my own projects to someone else | See "Handing a project over" below |

This tool contains no project data. It only turns **local sessions** into a source archive. Other people's sessions live on their machines; installing it here finds nothing.

---

## 🧠 Understand it in 30 seconds

Commands come in two groups — don't mix up their frequency:
**`scan` / `render` run a few times a month; `find` / `stats` are for every day.**

**First time (one-off)**

```bash
# 1. Inspect the classification — writes nothing. Run this first.
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan --dry-run

# 2. Happy with the table? Archive for real (scan + render)
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py run
```

**Everyday use (read-only, writes nothing)**

```bash
# Find things: project names / file names / prompt text, all words must match
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py find "batch convert"

# How much did this week cost you: prompts / projects / files / tokens
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py stats --days 7

# Something to show a colleague (single-file HTML)
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py report --days 30 --open

# What the archive looked like last Wednesday (preview first, --write to expand)
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py restore --date 2026-11-14
```

The first command only prints a table: how many project units were found, which are code projects, which are pure document tasks. **Run it first** and confirm the classification before anything is written.

The second, `run` = collect + render. When it finishes, open `INDEX.md` in the archive. Output defaults to `~/Workbuddy项目归档`; change it in one of two ways:

```bash
# Per command
python .../archiver.py run --target "D:\my-archive"

# Or an environment variable (Windows: set once, no flags afterwards)
[Environment]::SetEnvironmentVariable("ARCHIVER_TARGET", "D:\my-archive", "User")
```

---

## 🏗️ Pipeline

```mermaid
flowchart LR
  A["Local sessions<br/>~/.workbuddy"] --> B["Adapter<br/>7 neutral events"] --> C["Parse · classify"] --> D["Extract source<br/>drop noise"] --> E["Render<br/>git init"] --> G[("Fingerprints<br/>next run")]
```

The fingerprint store keeps three-level hashes (workspace / file / content). The next run compares before parsing, which is why a steady-state run makes zero model calls.

The one-liner:

```text
archive = read sessions + classify + drop noise + extract source + write docs + init repos + fingerprint
```

The real work is **deterministic parsing**. The model only handles the last step — turning a file listing and requirement history into readable prose. That division buys three things: reproducible output, no network dependency, and predictable cost.

---

## 🧩 Architecture: a thin adapter layer, a thick platform-agnostic core

Every platform difference lives in `adapters/`; the main program never sees another vendor's JSON:

```mermaid
flowchart TB
  subgraph AD["adapters/ · per platform · thin"]
    direction LR
    W["workbuddy.py"]
    CC["claude_code.py"]
    MINE["your_tool.py"]
    W ~~~ CC ~~~ MINE
  end
  E{{"7 neutral events"}}
  subgraph CORE["archiver.py · platform-agnostic · thick"]
    direction TB
    P["SessionParser"] --> X["classify · extract"] --> R["render<br/>git · state"]
  end
  AD --> E --> CORE
```

There is exactly one contract between adapters and the core — **7 neutral events**:

```text
ts / title / prompt / assistant / tool / write / tool_result
```

Adding a platform = one file in `adapters/` plus one `register()` call. The main program **does not change**. See "Adding another tool" below.

---

## 📐 Six design constraints

This is not a throwaway archiving script — every rule below was written after hitting a real problem:

| Constraint | How it is done | What it buys |
|---|---|---|
| Source only | Build output, dependencies, logs, screenshots, test samples and throwaway scripts are all excluded, each logged in `manifest.json` | A project drops from MBs to tens of KB ~ a few MB |
| Directory must stand alone | One independent git repo plus `.gitignore` per project | Zip and send it; `git clone` continues the work |
| Exclusions must be traceable | `manifest.json` records every exclusion and external dependency, plus `_evidence/` | When a file seems missing, check instead of guess |
| Rendering must be idempotent | Generation is pure-function style; two consecutive runs produce 0 changes | Safe to run on a nightly schedule |
| Incremental must be cheap | Three-level fingerprints (workspace / file / content sha1) | No repeated parsing or model calls in steady state |
| Must work without a model | Missing summary falls back to the `derive_from_manifest` template | A complete archive with zero API keys |

---

## 📦 What the output looks like

```
<archive>/
├── INDEX.md          # Master index: project / first dev / last dev / last update / type / language / sessions / files / purpose
├── index.json        # Machine-readable manifest
├── CHANGELOG.md      # Per-run change log (added / updated / lost / soft-deleted)
├── overrides.json    # Manual corrections: title / background / purpose, keep_scripts for false positives
├── state.json        # Fingerprint store — the basis for incremental sync
├── _trash/           # Directories no longer archived (soft delete, recoverable)
├── _restore/         # Historical snapshots produced by `restore` (archive untouched)
└── 2026-03-15_批量重命名工具/
    ├── README.md          # Background / features / implementation / source / sessions / overview
    ├── manifest.json      # Metadata + exclusion log + external dependency list
    ├── .gitignore  .git/  # Standalone repo, init + commit on first archive
    ├── open-session.bat  # Double-click to jump back to the latest session
    ├── source/            # Clean source, original relative paths preserved
    ├── _docs/             # Design documents (docx/pdf etc.)
    ├── _evidence/evidence.md
    ├── _meta/
    │   ├── conversations.md  # Every user instruction in this project, in order
    │   ├── integrity.md      # Project integrity check
    │   ├── sessions.json
    │   └── memory/           # Workspace project memory
    └── _summary.json      # Summary result (absent → template fallback)
```

**Directories are deliberately small**: only source, project files, config and necessary small assets. Every exclusion is logged for traceability; a typical project is tens of KB to a few MB.

---

## 🤖 Supported AI tools

| Tool | Session data | Status |
|---|---|---|
| WorkBuddy | `~/.workbuddy/` | ✅ Works out of the box, includes `workbuddy://` deep links |
| Claude Code | `~/.claude/projects/` | ✅ Works out of the box, degrades to a `claude --resume` hint when no deep link exists |
| Other local agents | Needs its own adapter | See "Adding another tool" |

Use `--source` to pick the source. The default `all` auto-detects what is installed; you can also pass `workbuddy`, `claude-code`, or a comma-separated combination. A tool with no local data is skipped silently instead of failing.

**Why web-based ChatGPT / Claude is not supported**: those services keep conversations in the cloud, so the code never lands on your disk as files. The archivist cannot obtain its single most important deliverable — the complete source. This tool targets **agent-style tools that write files locally**.

### 🔌 Adding another tool

Each file under `scripts/adapters/` maps to one platform. Implement an `Adapter` subclass and call `register()` at the end of `__init__.py`:

```python
class MyAdapter(Adapter):
    name = "my-tool"           # also the value for --source
    display = "My Tool"
    def probe(self) -> bool: ...               # does this platform have data here?
    def load_index(self): ...                  # returns Index: ids() / info(sid) / transcript(sid)
    def iter_events(self, path, cwd=""): ...   # yields the 7 neutral events
```

The event contract and ready-made helpers live at the top of `scripts/adapters/base.py`. Two hard rules: `created` / `updated` in `info()` must be **epoch milliseconds** (otherwise cross-platform sorting breaks), and tool paths must go through `rel_in_cwd()`. After editing, run `python scripts/tests/test_adapters.py`.

---

## 🚀 Quick start

### 0. Requirements

| Item | Requirement |
|---|---|
| OS | Windows / macOS / Linux |
| Python | 3.8+ (**no pip install needed**, standard library only) |
| AI tool | Local session data from at least one supported tool, see above |
| git | Optional. Needed to give each project its own repository; without it that step is skipped and everything else still works |

### 1. Install

**Option 1: git clone (recommended, easy to update later)**

```bash
# WorkBuddy
git clone https://github.com/winrich/dev-project-archivist.git ~/.workbuddy/skills/dev-project-archivist

# Claude Code
git clone https://github.com/winrich/dev-project-archivist.git ~/.claude/skills/dev-project-archivist
```

**Option 2: zip archive**

Unzip and place the whole `dev-project-archivist` folder into the skills directory of the target tool, so the final path is:

```
~/.workbuddy/skills/dev-project-archivist/SKILL.md     # WorkBuddy
~/.claude/skills/dev-project-archivist/SKILL.md        # Claude Code
```

**Restart the tool** after installing for it to appear in the skill list. You can also skip the skill entirely and run the CLI — the archiving logic does not depend on any host tool.

### 2. Run it

```bash
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan --dry-run   # classification only
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py run              # archive for real
```

> Commands assume the WorkBuddy install path. If installed under Claude Code, replace `~/.workbuddy/skills/` with `~/.claude/skills/`.

### 3. Use it in a conversation

After restarting, simply tell the assistant:

> Archive the projects I built across my conversations.

The skill is picked up automatically and runs the same pipeline.

---

## 🆓 Zero-cost mode (no LLM required)

**LLM summarisation is optional.** Skip it and the archive is still complete — project names fall back to folder names, and feature notes are derived from the file listing:

```bash
python .../archiver.py run
```

`run` does not include summarisation; rendering automatically uses the **template fallback** (`derive_from_manifest`). It suits three cases: you do not want an API key or any token cost; you just want the source collected now and the prose later; a one-off run on someone else's machine.

**Upgrading to LLM summaries later is easy**: write `_summary.json` for a project and re-run `render` — no re-scan needed. Conversely, delete `_summary.json` and re-render to fall back to the template.

> There is no `--no-llm` switch. Whether a model is called depends purely on whether you ran the summarisation step.

**When is a model worth it**: when you have many projects and want readable names plus coherent feature descriptions. A nightly job only summarises projects that changed, so in steady state the model call count is 0.

---

## 🔗 Going back to the original conversation

The jump mechanism depends on the session's source; tools without deep-link support never get a broken link forced onto them.

| Platform | How to open |
|---|---|
| WorkBuddy | Click "打开对话" in the README, or run `open-session.bat` in the project root (`.command` on macOS, `.sh` on Linux) |
| Claude Code | `open-session.md` in the project root records the session ID and a `claude --resume <ID>` command |

Regardless of platform, every project has `_meta/conversations.md` — all user instructions for that project in chronological order, so you can see the requirement evolution without jumping anywhere.

---

## 📚 Command reference

| Command | What it does |
|---|---|
| `scan` | Collect / extract / copy source, write the evidence pack and the pending-summarisation list |
| `scan --dry-run` | Print the classification table only, zero writes (**use first to calibrate**) |
| `scan --dry-run --all` | Also list empty-shell sessions (collapsed by default) |
| `scan --explain [KEYWORD]` | Print why a project was classified this way and where its name came from (read-only) |
| `scan --force` | Ignore fingerprints and rescan (after switching machines or changing rules) |
| `scan --only KEYWORD` | Only process projects matching the keyword in name / path / session title / archive dir |
| `scan --limit N` | Only process the N most recent projects |
| `render` | Generate READMEs and the master index from summaries (idempotent) |
| `run` | scan + render |
| `--source all\|workbuddy\|claude-code` | Choose data source(s). Default `all` auto-detects; comma-separated allowed |
| `pending` | List projects whose evidence changed and need re-summarising |
| `status` | Last run time, lost projects, projects missing summaries, `.git` size check |
| `git-clean` | Rebuild the repositories this tool created, purging large objects from history |
| `find KEYWORD` | **Cross-project search**: names / file names / prompt text. All words must match. Read-only |
| `find KEYWORD --open` | Open the original conversation of the top hit |
| `find KEYWORD --json` | Machine-readable output (score + snippets) |
| `stats --days 7` | **Cross-day ledger**: prompts / projects / files / tokens for the last N days, plus a daily bar (read-only; `--date` sets the end date, `--json` for scripts) |
| `report --days 30` | **Write a single-file HTML ledger**: heatmap + bar chart + project cards. No paths, no prompt text — safe to share (`--out` dir, `--open` to launch) |
| `restore` | List **which days you can go back to** (days on which the archive repos have commits) |
| `restore --date 2026-11-14` | **Preview** the snapshot as of that day's end: projects / files / size, zero writes |
| `restore --date 2026-11-14 --write` | Expand a copy into `<target>/_restore/2026-11-14/` — **never overwrites the archive** |
| `restore --date 2026-11-14 --write --only KEYWORD` | Restore only matching projects |
| `restore --date 2026-11-14 --json` | Machine-readable output |

---

## ⏪ Going back to a given day: `restore`

An archive is worth more than "one more look" — it is worth **being able to rewind**. Every project
directory is its own git repo with one commit per scan, so "what did the archive look like on 14 Nov"
is a question with an exact answer:

```bash
python .../archiver.py restore                            # 1. which days can I go back to
python .../archiver.py restore --date 2026-11-14          # 2. preview, zero writes
python .../archiver.py restore --date 2026-11-14 --write  # 3. expand a copy for real
```

### Three semantics — read these before the flags

| It is | It is not |
|---|---|
| Taken **only from each archive repo's own git history** | Not taken from the original workspace — which may be long deleted, renamed or overwritten |
| "What the archive looked like **at the end of that day**" | **Not "what changed that day"** — for that use `log` / `stats` |
| Only files that were **already archived at the time** | Not a backup. Media, oversized binaries, sensitive paths, `.gitignore`-excluded build output, and anything **before the first archive** were never in git and cannot be conjured back |

Two boundaries follow, and both are reported explicitly rather than silently skipped:

- **No scan ran that day** → there is no snapshot for it; `restore` falls back to the **most recent earlier commit** and prints that commit's real timestamp, never pretending it was that day
- **A project was first archived after the target date** → it is listed under "cannot restore" with the reason: `no archive before that date (first archived 2026-11-16)`

### It never overwrites the archive

The classic way to get "go back to a day" wrong is to overwrite the current archive in place, so this is
a hard constraint: `restore` only **reads** the archive repos (`git archive`, never touching the working
tree or the index) and writes into a fresh directory.

| Flag | Behaviour |
|---|---|
| default (no `--write`) | Preview only: which commit each project resolves to, file count, size — **zero writes** |
| `--write` | Expand into `<target>/_restore/<date>/<project>/`; the archive is byte-for-byte unchanged |
| `--out DIR` | Write the copy somewhere else |
| `--only KEYWORD` | Restore only matching projects |
| `--force` | Overwrite an existing copy for the same day; confirm a total above 800 MB |
| `--json` | Machine-readable (`commit` / `snapshot_at` / `files` / `bytes` per project) |

Each expanded copy carries `_restore.md` + `_restore.json` recording **which commit** every project came
from, so the result can be audited later. The copy has no `.git` — open it or build it directly.

> Need to rewind **one** project in place? You don't need this command: the archive directory is already
> a repo — `cd` into the project, pick a commit in `git log` and `git checkout <sha> -- .`.
> `restore` is for the other job: getting a copy you can open and build without touching today's archive.

---

## 🌙 Daily automation (optional)

Create a scheduled task in your AI tool (WorkBuddy automation, system scheduler — any works) with a three-step prompt: `scan` → read `pending` and summarise each → `render`. A complete copy-pasteable prompt is in [`references/automation-prompt.md`](references/automation-prompt.md).

Key points:

- Only projects whose **fingerprint changed** enter `pending`, so the model call count is 0 on stable days
- Schedule it off-peak and bind it to a cheaper model
- **Always pass `--target` explicitly in scheduled jobs** — do not rely on the environment variable

---

## ❓ FAQ

**Nothing is found**
Confirm that `.jsonl` transcripts exist under `~/.workbuddy/projects/` (or `~/.claude/projects/`). A fresh install with no prior conversations has nothing to archive.

**Classification is wrong — a chat got treated as a project**
Run `scan --dry-run` first and look at the table. The rule is "source files ≥ 5 and at least 30% of the document count". Tune with `--min-code N`; to permanently exclude a workspace, set `skip: true` for it in `overrides.json`.

**Wrong project name or description**
Override `title` / `background` / `purpose` in `overrides.json` by workspace path or archive directory name, then re-run `render`.

**A core script was excluded by mistake**
The tool drops one-off scripts shaped like `repro_*`, `check_*`, `*_bak`. If it hits a real file, add `{"keep_scripts": ["name.py"]}` to `overrides.json` and re-run `scan --force`.

**A project keeps saying "no changes" although it did change**
`scan --force --only KEYWORD`.

**The index shows ⚠️ lost**
The original workspace was deleted; the archive and its source copy are still intact.

**The project directory is larger than expected**
Check `excluded.external` in `manifest.json` and the large files under `source/`. Defaults: 5 MB per file, 40 MB of source per project, 256 KB resource threshold — anything above is logged rather than copied.

**The git repository is suspiciously large**
After changing exclusion rules, old large blobs may remain in history: `python .../archiver.py git-clean`.

---

## 📤 Handing a project over

Every project in the archive is an **independent git repository** — just zip it or push it:

```bash
cd <archive>/2026-03-15_批量重命名工具
git remote add origin <your-remote-url>
git push -u origin master
```

Whoever clones it can pick up where you left off using the "Implementation" section of `README.md` and the integrity report in `_meta/integrity.md`.

---

## 📁 Repository layout

```text
dev-project-archivist/
├── SKILL.md                        # Skill entry: trigger conditions and execution rules
├── README.md  README.en.md         # Chinese / English docs
├── CHANGELOG.md                    # Release history and versioning rule
├── pyproject.toml                  # Installable via pipx / uvx (still zero dependencies)
├── .github/workflows/ci.yml        # 2 OS x 3 Python versions, full unittest discover
├── scripts/
│   ├── archiver.py                 # Core: archive / log / find / stats / report / restore
│   ├── adapters/
│   │   ├── base.py                 # Neutral event contract, Adapter base, multi-source index
│   │   ├── workbuddy.py            # WorkBuddy session index and parser
│   │   ├── claude_code.py          # Claude Code JSONL parser
│   │   └── __init__.py             # Registry and --source selection
│   └── tests/                      # 210 tests, all runnable offline
│       ├── test_adapters.py        # Adapter unit tests
│       ├── test_daylog.py          # Per-day aggregation (nothing lost or double counted)
│       ├── test_naming.py          # Name evidence chain and title cleanup
│       ├── test_find.py            # Search scoring and per-kind saturation
│       ├── test_commit_msg.py      # Commit message generation
│       ├── test_stats.py           # Cross-day ledger, same numbers as daylog
│       ├── test_report.py          # HTML report structure and redaction
│       ├── test_restore.py         # Restore semantics (throwaway git repos)
│       ├── test_release.py         # Version number consistent in all three places
│       └── test_public_content.py  # Leak guard: competitor name / home paths with a username
├── references/
│   ├── automation-prompt.md        # Copy-pasteable prompt for the nightly job
│   └── data-sources.md             # Local data source structures (troubleshooting)
└── LICENSE                         # MIT
```

---

## 🤝 Contributing

Issues and PRs are welcome. Especially useful:

- Adapters for other AI tools (Codex CLI / Cline / Roo Code / Cursor)
- Fixes for file rules that wrongly exclude or include something
- Readability improvements to the rendered READMEs
- Wording fixes in the English docs

Before submitting, please run:

```bash
python -m unittest discover -s scripts/tests   # full suite, 210 tests, offline
python scripts/archiver.py scan --dry-run
```

Changes go into [CHANGELOG.md](CHANGELOG.md). Its header states the rule: the version equals the highest
completed milestone, and `pyproject.toml` / `__version__` / the top CHANGELOG entry must agree
(`test_release.py` fails otherwise).

---

## 📄 License

[MIT](LICENSE)

---

<p align="center">
  ⭐ <b>If this project helped you, a star is appreciated.</b> ⭐
</p>
