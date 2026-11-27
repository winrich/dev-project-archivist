# Contributing

Thanks for helping improve this tool. Contributing needs no network access, no account, and no third-party packages — **Python standard library + git is enough**.

[中文版本](CONTRIBUTING.md)

---

## 1. The three most valuable contributions

| Type | Why it matters | Entry point |
|---|---|---|
| **An adapter for a new platform** | Each supported AI tool unlocks a whole new group of users. This is the only repeatable growth lever | [Adapter issue template](.github/ISSUE_TEMPLATE/adapter.md) |
| **File-extraction rule fixes** | A wrongly excluded or missed file directly breaks "clone and keep working" | [Bug report template](.github/ISSUE_TEMPLATE/bug.md) |
| **Documentation clarity** | Wording, examples, command semantics. Good docs help more people than one extra command | Open a PR directly |

An adapter does not touch the main pipeline: implement `probe()` / `load_index()` / `iter_events()` / `deep_link()` per `scripts/adapters/base.py`, then register it in `scripts/adapters/__init__.py` (two steps). Field semantics are in [DEVELOPMENT.en.md](DEVELOPMENT.en.md), section "Adapter interface".

> **Hard rule**: never guess the schema. An adapter must be built on real session samples — install the platform locally, produce real conversations, run `probe()` to confirm the structure, then write code with a fixture from those samples. Guessing yields zero parsed events for users, which is worse than not supporting the platform.

## 2. Local development setup

```bash
git clone https://github.com/winrich/dev-project-archivist.git
cd dev-project-archivist
python --version    # 3.8+
git --version       # optional; used to init one git repo per archived project
```

Zero third-party dependencies, no `pip install` needed. Tests use standard-library `unittest` and run fully offline.

## 3. Pre-submit checks (all required)

```bash
python -m unittest discover -s scripts/tests   # full suite, offline
python scripts/tests/test_public_content.py     # public-content leak check
python scripts/archiver.py scan --dry-run       # archive pipeline smoke test
```

| Check | Why |
|---|---|
| Full test suite green | 250 tests cover naming, find, sync, report, manage UI; changes often break something elsewhere |
| Public-content check | Every byte here is pushed to GitHub; no real usernames in paths, real project names, or competitor names |
| `scan --dry-run` | Confirms the main pipeline still works and writes nothing |

## 4. Three red lines for public content

This repository is public; every commit is visible to everyone. `scripts/tests/test_public_content.py` automatically blocks two of the following; the third relies on you:

1. **No competitor names.** You do not need to name another product to explain a difference — the test blocks this.
2. **No absolute paths with a real username.** Replace `C:/Users/<user>/...` with a whitelisted placeholder or a fictional path — blocked by the test.
3. **No real project names or real usage numbers.** `BatchRenamer` / `InvoiceParser` in the README are fictional; all sample data is invented. Real names expose your working directories and business context. This one relies on you, because a deny-list would itself spell out what is being hidden.

## 5. Pull request guidelines

- One PR per change so it can be reverted cleanly. Open an issue first for anything substantial, especially a new adapter.
- Title with a conventional prefix: `feat:` / `fix:` / `docs:` / `test:` / `chore:`.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md` with the change description and self-check list.
- Add a line to [CHANGELOG.md](CHANGELOG.md) (documentation-only changes may skip this).
- Versioning rule: **the version equals the highest completed tier** — never announce unfinished work, never lag behind. `pyproject.toml` version, `scripts/archiver.py` `__version__`, and the newest `CHANGELOG.md` heading must match; `test_release.py` enforces this.

## 6. Reporting problems

Before opening an issue, run `python scripts/archiver.py scan --dry-run` and paste **only the classification table** — never raw conversation text, which may contain your project paths and prompts.

For security issues, do not open a public issue; see [SECURITY.md](SECURITY.md).

## 7. Code of conduct

Respect every contributor. Discuss technical disagreements on the merits; no personal attacks and no disparaging other tools' users. Maintainers may close off-topic issues or PRs that violate the red lines above, with a stated reason.
