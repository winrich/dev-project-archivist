# Welcome to the Dev Project Archivist discussions

This is the place to share how you use the tool, ask questions, and show what you found. Please skim the notes below first — they save a round trip of clarification.

## What problem does it solve

Software built inside AI conversations often becomes impossible to locate three months later. This tool scans local session data, identifies projects you actually developed, and collects **pure source code** (no dependencies, build artifacts, or screenshots) into archives you can `git clone` and keep working on — with a deep link back to the original conversation.

## Three steps to get started

```bash
git clone https://github.com/winrich/dev-project-archivist.git ~/.workbuddy/skills/dev-project-archivist
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py scan --dry-run
python ~/.workbuddy/skills/dev-project-archivist/scripts/archiver.py find a-keyword-you-remember
```

Check the classification table first, then decide whether to actually write to disk. No network access, no third-party dependencies.

## Before you post

- **Do not paste raw conversation text** — it may contain your project paths and prompts. When reporting a problem, paste only the `scan --dry-run` table.
- **Do not paste absolute paths with a real username.** Use a fictional name or placeholder.
- **Do not report security vulnerabilities here** — use the private report channel in [SECURITY.md](../../SECURITY.md).

## What we welcome

| Category | Good posts |
|---|---|
| **Show and tell** | What project you recovered, how many you archived, anything unexpected |
| **Q&A** | Installation, command semantics, a file type wrongly excluded or missed |
| **Adapters** | A new platform you want supported, plus its real (redacted) session-data structure |
| **Ideas** | Features you want, things that feel awkward, docs you could not follow |

## Where to start

- [README](../../README.md): installation, commands, FAQ
- [DEVELOPMENT.md](../../DEVELOPMENT.md): architecture and command semantics
- [CONTRIBUTING.md](../../CONTRIBUTING.md): how to open a PR and the three public-content red lines

Maintainers will try to reply, but for new issues please open a tracked Issue (templates available) so it can be followed and reverted cleanly.
