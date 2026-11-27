# Security Policy

## Supported versions

| Version | Security reports accepted |
|---|---|
| Latest 0.4.x release | ✅ |
| Earlier versions | ❌ Please upgrade to the latest release first |

This is a **local-only tool**: no network access, no account, no upload of any conversation content. Security concerns therefore focus on "does local data accidentally get written out" and "does the archive process execute anything it shouldn't".

## Reporting a vulnerability

**Do not open a public issue.** A public issue exposes the details and puts users at risk.

Use GitHub's private vulnerability report: open the repository's **Security** tab → **Report a vulnerability**. Include:

- The affected version (output of `python scripts/archiver.py --version`)
- Reproduction steps (use fictional data; do not paste real conversations or real paths)
- The potential impact

Maintainers will respond after confirming. Please do not disclose details publicly until a fix is released.

## What this project explicitly does not do

- **No network access**: the main pipeline makes no network requests and sends no telemetry.
- **No conversation uploads**: all transcript parsing, summarization, and rendering happen locally. Optional LLM summarization sends only a compressed evidence package, configured by the user.
- **No execution of archived content**: the archiver reads file-write records from conversations and copies text by path; it never executes that code.
- **No overwriting hand-written content**: READMEs have `AUTO` and `USER-NOTE` sections; hand-written text survives re-archiving.

## What users should guard themselves

1. **Permissions on the archive directory**: it contains complete source code; do not put it in a world-readable shared location.
2. **`ARCHIVER_TARGET` value**: confirm it points to your own directory, not a path on someone else's machine.
3. **`sync --push` remotes**: it only pushes to each project repo's **own configured** remote and never forces; run `sync` first to review the list.

---

[中文版本](SECURITY.zh.md)
