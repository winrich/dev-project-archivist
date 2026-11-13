# Conversation Project Archivist

**English** | [简体中文](README.md)

Turn software projects scattered across hundreds of AI chat windows into a browsable, handover-ready, continuously syncable local source archive.

Every "software built through conversation" leaves behind its own session. After a few months you can no longer find where the source went, or why you made the choices you made. This tool reads the local session data of AI coding agents (WorkBuddy / Claude Code), identifies the projects you actually built in each conversation, and archives them per project: **clean source you can keep developing**, project background, per-module feature notes, technical implementation notes, plus a one-click link back to the original conversation.

---

## First, decide: do you need this tool, or a specific project?

| Your goal | What to do |
|---|---|
| I use AI coding agents too, and want to collect **my own** conversation projects | Install this tool (keep reading) |
| I just want to take over someone's finished project and keep developing it | **Do not install the tool.** Take that project folder (with `source/`, `.git/`, `README.md`) and `git clone` or unzip it |
| I want to hand a project of mine to someone else | See [Handing a project over](#handing-a-project-over) |

This tool contains no project data. It only turns local sessions into a source archive. Other people's sessions live on their machines — installing it here would find nothing.

---

## Requirements

| Item | Requirement |
|---|---|
| OS | Windows / macOS / Linux |
| Python | 3.8+ (**no pip install needed**, standard library only) |
| AI tool | Local session data from at least one supported tool — see below |
| git | Optional. Needed to give each project its own repository; without it that step is skipped and everything else still works |

---

## Supported AI tools

| Tool | Session data location | Status |
|---|---|---|
| WorkBuddy | `~/.workbuddy/` | ✅ Works out of the box, includes `workbuddy://` deep links |
| Claude Code | `~/.claude/projects/` | ✅ Works out of the box, degrades to a `claude --resume` hint when no deep link exists |
| Other local agents | Needs its own adapter | See [Adding another tool](#adding-another-tool) |

Use `--source` to pick the source. The default `all` auto-detects what is installed; you can also pass `workbuddy`, `claude-code`, or a comma-separated combination. A tool with no local data is skipped silently instead of failing.

**Why web-based ChatGPT / Claude is not supported**: those services keep conversations in the cloud, so the code never lands on your disk as files. The archivist cannot obtain its single most important deliverable — the complete source. This tool targets **agent-style tools that write files locally**.

### Adding another tool

Each file under `scripts/adapters/` maps to one platform. Implement an `Adapter` subclass and call `register()` at the end of `__init__.py` — the main program needs zero changes:

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

## Installation

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

**Restart the tool** after installing for it to appear in the skill list. You can also skip the skill entirely and just run the CLI — the archiving logic does not depend on any host tool.

---

## Three steps to start

> Commands below assume the WorkBuddy install path. If installed under Claude Code, replace `~/.workbuddy/skills/` with `~/.claude/skills/`.

### 1. Inspect the classification first — writes nothing

```bash
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan --dry-run
```

It prints a table: how many project units were found, which are classified as code projects, which are pure document tasks. **Always run this first** and confirm the classification looks right before writing anything.

### 2. Archive for real

```bash
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py run
```

`run` = collect + render. When it finishes, open `INDEX.md` in the archive directory.

Output defaults to `~/Workbuddy项目归档`. To change it, pick one:

```bash
# Per-command
python .../archiver.py run --target "D:\my-archive"

# Or an environment variable (Windows: set once, no flags afterwards)
[Environment]::SetEnvironmentVariable("ARCHIVER_TARGET", "D:\my-archive", "User")
```

### 3. Use it in a conversation

After restarting, simply tell the assistant:

> Archive the projects I built across my conversations.

The skill is picked up automatically and runs the same pipeline.

---

## Zero-cost mode (no LLM required)

**The "LLM summarisation" step is optional.** Skip it and the archive is still complete — project names fall back to folder names, and feature notes are derived from the file listing:

```bash
python .../archiver.py run
```

`run` does not include summarisation; rendering automatically uses the **template fallback** (`derive_from_manifest`). This suits:

- You do not want to configure an API key or pay for tokens
- You just want the source collected now and the prose later
- A one-off run on someone else's machine

**Upgrading to LLM summaries later is easy**: write `_summary.json` for a project and re-run `render` — no re-scan needed. Conversely, delete `_summary.json` and re-render to fall back to the template.

> There is no `--no-llm` switch. Whether a model is called depends purely on whether you ran the summarisation step.

**When is a model worth it**: when you have many projects and want readable names plus coherent feature descriptions. A nightly job only summarises projects that changed, so in steady state the model call count is 0.

---

## What the output looks like

```
<archive>/
├── INDEX.md          # Master index: project / first dev / last dev / last update / type / language / sessions / files / purpose
├── index.json        # Machine-readable manifest
├── CHANGELOG.md      # Per-run change log (added / updated / lost / soft-deleted)
├── overrides.json    # Manual corrections: title / background / purpose, keep_scripts for false positives
├── state.json        # Fingerprint store — the basis for incremental sync
├── _trash/           # Directories no longer archived (soft delete, recoverable)
└── 2026-03-15_批量重命名工具/
    ├── README.md          # Background / features / implementation / source / sessions / overview
    ├── manifest.json      # Metadata + exclusion log + external dependency list
    ├── .gitignore  .git/  # Standalone repo, init + commit on first archive
    ├── 打开原对话.bat      # Double-click to jump back to the latest session
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

**Directories are deliberately small**: only source, project files, config and necessary small assets. Build outputs, dependencies, logs, generated reports, screenshots, test sample photos and throwaway diagnostic scripts are all excluded, and every exclusion is logged in `manifest.json` for traceability. A typical project is tens of KB to a few MB.

---

## Going back to the original conversation

The jump mechanism depends on the session's source; tools without deep-link support never get a broken link forced onto them.

| Platform | How to open |
|---|---|
| WorkBuddy | Click "打开对话" in the README, or double-click `打开原对话.bat` in the project root |
| Claude Code | `打开原对话.md` in the project root records the session ID and a `claude --resume <ID>` command |

Regardless of platform, every project has `_meta/conversations.md` — all user instructions for that project in chronological order, so you can see the requirement evolution without jumping anywhere.

---

## Command reference

| Command | What it does |
|---|---|
| `scan` | Collect / extract / copy source, write the evidence pack and the pending-summarisation list |
| `scan --dry-run` | Print the classification table only, zero writes (**use first to calibrate**) |
| `scan --force` | Ignore fingerprints and rescan (after switching machines or changing rules) |
| `scan --only KEYWORD` | Only process projects matching the keyword in name / path / session title / archive dir |
| `scan --limit N` | Only process the N most recent projects |
| `render` | Generate READMEs and the master index from summaries (idempotent) |
| `run` | scan + render |
| `--source all\|workbuddy\|claude-code` | Choose data source(s). Default `all` auto-detects; comma-separated allowed |
| `pending` | List projects whose evidence changed and need re-summarising |
| `status` | Last run time, lost projects, projects missing summaries |
| `git-clean` | Rebuild the repositories this tool created, purging large objects from history |

---

## Daily automation (optional)

Create a scheduled task in your AI tool (WorkBuddy automation, system scheduler, cron — any works) with a three-step prompt: `scan` → read `pending` and summarise each → `render`.
A complete copy-pasteable prompt is in [`references/automation-prompt.md`](references/automation-prompt.md).

Key points:
- Only projects whose **fingerprint changed** enter `pending`, so the model call count is 0 on stable days
- Schedule it off-peak and bind it to a cheaper model
- **Always pass `--target` explicitly in scheduled jobs** — do not rely on the environment variable

---

## FAQ

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

## Handing a project over

Every project in the archive is an **independent git repository** — just zip it or push it:

```bash
cd <archive>/2026-03-15_批量重命名工具
git remote add origin <your-remote-url>
git push -u origin master
```

Whoever clones it can pick up where you left off using the "Implementation" section of `README.md` and the integrity report in `_meta/integrity.md`.

---

## Repository layout

| File | Purpose |
|---|---|
| `SKILL.md` | Skill entry point: trigger conditions and execution rules |
| `scripts/archiver.py` | All pipeline logic (~2300 lines, standard library only) |
| `scripts/adapters/` | One file per platform, plus the neutral event contract in `base.py` |
| `scripts/tests/test_adapters.py` | Adapter unit tests |
| `references/automation-prompt.md` | Copy-pasteable prompt for the nightly job |
| `references/data-sources.md` | Local data source structures (for troubleshooting) |

---

## License

[MIT](LICENSE)
