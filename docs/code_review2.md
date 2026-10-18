# Code Review 2: hf-lmstudio

Reviewer: automated code review (OpenCode)
Date: 2026-10-18
Scope: whole repository, with a deep dive on `src/hf_lmstudio/cli.py`.
Tree reviewed: `code_review` branch working tree (post fix-round:
`cli.py` 543 lines, `tests/test_cli.py` 550 lines,
`pyproject.toml`, `.github/workflows/test.yml`, `docs/*`).

This is a **second, independent review**, produced after
`docs/code_review.md` (round 1) and the fix round that closed findings
F1–F10, R1–R9 and N1–N8. It does **not** repeat those findings. It records
**new** findings on the current tree, each with evidence, and ends with a
prioritised list of recommended changes. This document changes no behaviour.

---

## 1. Method and standards used

The review combined (a) a line-by-line read of the repository, (b) empirical
probes of `cli.py` against temporary sandboxes, and (c) the following public
design/coding standards, located by web search:

- **Command Line Interface Guidelines (clig.dev)** — exit zero on success /
  non-zero on failure; "send output to `stdout`, send messaging to `stderr`";
  only prompt when `stdin` is a TTY; "never _require_ a prompt"; "confirm
  before doing anything dangerous"; make operations idempotent; disable
  colour/ANSI when not a TTY; rewrite errors for humans.
  <https://clig.dev/>
- **12 Factor CLI Apps (Jeff Dickey, Heroku)** — "mind the streams"
  (`stdout` = output, `stderr` = messaging); great help; a discoverable
  version; informative errors; follow XDG; prompt only when you can.
  <https://medium.com/@jdxcode/12-factor-cli-apps-dd3c227a0e46>
- **PEP 8 – Style Guide for Python Code** — "mention specific exceptions
  whenever possible instead of using a bare `except:`"; naming; module
  docstrings. <https://peps.python.org/pep-0008/>
- **PEP 20 – The Zen of Python** — "Explicit is better than implicit",
  "Errors should never pass silently. Unless explicitly silenced.",
  "In the face of ambiguity, refuse the temptation to guess."
  <https://peps.python.org/pep-0020/>
- **PEP 257 – Docstring Conventions**; **PEP 484 / 585 / 604 – typing**
  (`X | Y`, `list[...]`). <https://peps.python.org/pep-0257/>,
  <https://peps.python.org/pep-0484/>
- **PEP 561 – py.typed marker** — a package that ships annotations should
  ship `py.typed` for downstream/editors. <https://peps.python.org/pep-0561/>
- **PEP 621 – project metadata** — `optional-dependencies` values **must** be
  arrays of PEP 508 strings; `license`/`classifiers`/`readme` belong to
  `[project]`. <https://peps.python.org/pep-0621/>
- **Google Python Style Guide** — type-annotated code, docstrings, naming.
  <https://google.github.io/styleguide/pyguide.html>
- **Ruff lint rules** — `BLE001` (blind-except), `TRY` (exception
  anti-patterns), `SIM`, `PTH` (use `pathlib`), `S` (flake8-bandit),
  `RUF`, `E`/`F`/`B`/`PLR`. <https://docs.astral.sh/ruff/rules/>
- **Python stdlib docs** — `pathlib`, `os.walk` (`followlinks=False` by
  default), `os.symlink`, `argparse`, **Logging HOWTO** (logging goes to
  stderr by default). <https://docs.python.org/3/library/os.html>,
  <https://docs.python.org/3/howto/logging.html>
- **Common Weakness Enumeration** — **CWE-22** path traversal,
  **CWE-59** link following, **CWE-367** TOCTOU.
  <https://cwe.mitre.org/data/definitions/22.html>
- **Project docs** — `docs/SPEC.md` is the declared source of truth;
  `docs/huggingface-cache.md` and `docs/lmstudio-cache.md` describe the two
  on-disk layouts involved.

---

## 2. Summary

The code remains small, readable, well-commented and well-tested at the unit
level (39 stdlib `unittest` tests, all passing, no network). The
`ModelFormat` enum, `NamedTuple` records, narrow-ish exception handling and
lazy `HfApi` use are all solid. Round 1's findings are genuinely addressed.

However, the round-1 review concentrated on *classification*. This review
concentrates on **filesystem mutation safety, discovery robustness and CLI
contract**, where real defects remain. The most important ones:

1. **`cleanup_dangling_links()` deletes empty directories it never created**,
   including user directories unrelated to the HF cache — a data-loss risk
   and a direct violation of SPEC §2.6 ("directories must not be removed
   unless they previously contained a symlink to the hf cache").
2. **`_unlink_model()` / `_link_model()` act on symlinks the tool did not
   create** — they will remove or replace a user's own symlink at a model
   path, contrary to "only symlinks we created".
3. **Path traversal:** a crafted HF-cache directory name yields a
   `model_name` like `../../escaped`, so linking writes **outside** the LM
   Studio directory.
4. **Discovery silently drops valid models**: `refs/*` content is not
   stripped, so a ref file ending in a newline is treated as a missing
   snapshot; and snapshot selection is order-of-`glob` dependent, so a PR/tag
   ref can win over `main`.
5. **Interactive UI crashes on an empty cache** (`IndexError` on SPACE/↓),
   and it calls `sys.exit(0)` from deep inside a library function.
6. **Packaging bug:** in `pyproject.toml`, `license` and `classifiers` are
   accidentally nested under `[project.optional-dependencies]` (and
   `license = {text = …}` there is invalid PEP 621), so the declared MIT
   licence and classifiers are not part of the distribution metadata.

Repo-wide themes: status messages funnel through two different channels
(`_warn` → `stderr`, `logger` → root logger), the CLI has no `--version`,
cancellation exits `0`, and the tests do not cover any of the destructive
paths above.

Every finding below was reproduced against the current tree; the raw probe
output is reproduced in §7.

---

## 3. Findings — filesystem mutation and discovery

### C1 (High, correctness/data-loss) — `cleanup_dangling_links()` deletes user-created empty directories

**Location:** `src/hf_lmstudio/cli.py:510-518` (prune loop).

The cleanup pass removes dangling HF-cache links (correct) and then removes
**every** empty directory under `lm_dir`:

```python
# Prune empty directories left behind, never removing lm_dir itself.
for root, _dirs, _files in os.walk(lm_dir, topdown=False):
    root_path = Path(root)
    if root_path == lm_dir:
        continue
    try:
        root_path.rmdir()  # succeeds only when the directory is empty
    except OSError:
        pass
```

There is no record of which directories previously held a symlink into the
HF cache, so a pre-existing, user-created empty directory is deleted.

**Evidence (probe 2):** creating `lm_dir/my-own-notes/` (never linked from
the cache) and calling `cleanup_dangling_links()` removes it —
`user empty dir still exists? False`.

This contradicts SPEC §2.6 line 73 ("directories must not be removed unless
they previously contained a symlink to the hf cache") and the safety rule
"never modify or delete existing user files".

**Recommended change**
- Track, during the first pass, the set of directories that **contained a
  removed/targeted symlink**, and only `rmdir()` those (bottom-up). Do not
  prune arbitrary empty directories.
- Add a regression test asserting a user-created empty directory survives
  `cleanup_dangling_links()`.

### C2 (High, correctness/data-loss) — Link/unlink act on symlinks the tool did not create

**Location:** `src/hf_lmstudio/cli.py:440-456` (`_unlink_model`) and
`420-437` (`_link_model`).

`_unlink_model` removes *any* symlink at `lm_dir/model_name` and *any*
symlink child inside that directory:

```python
if target_path.is_symlink():
    target_path.unlink()
elif target_path.is_dir():
    for child in list(target_path.iterdir()):
        if child.is_symlink():
            child.unlink()
```

It does not check that the symlink points into `hub_dir`, or that the tool
created it. Likewise `_link_model` unlinks an existing symlink at the target
before rebuilding, even if that symlink is the user's own.

**Evidence (probe 3):** a user symlink `lm_dir/org/model-a -> /tmp/.../my_precious_model`
is silently unlinked by `apply_desired_state(..., desired=set())` —
`user symlink still exists? False`.
**Evidence (probe 7):** a user symlink-to-directory at the target is replaced
by a directory of links — `target now a dir of symlinks? True`.

This conflicts with SPEC §2.6 ("existing symlinks may be updated, created, or
removed" refers to links *we* create) and `Design.md`'s promise that only our
symlinks are dropped.

**Recommended change**
- Before unlinking a symlink, verify it resolves inside `hub_dir`
  (`os.path.realpath(...)`), as `cleanup_dangling_links()` already does; skip
  and warn otherwise.
- Track the manifest of links created per model (e.g. a small sidecar, or
  compare each child link target against the snapshot) so unlink only removes
  links pointing at the corresponding snapshot file.
- Add tests for "user symlink to an external path is preserved".

### C3 (Medium, security) — Path traversal via crafted cache directory name

**Location:** `src/hf_lmstudio/cli.py:304` (repo-id derivation) used at
`350`, `422`, `442`.

`model_name` is derived from the cache folder name by string replacement and
is then used directly as a path component:

```python
model_name = model_dir.name.removeprefix("models--").replace("--", "/")
...
target_path = lm_dir / model_name
```

Nothing rejects `..`, an absolute prefix, or backslashes. A locally present
directory `models--..--..--escaped` produces `model_name == "../../escaped"`.

**Evidence (probe 8):** with such a cache entry, `apply_desired_state()`
created a directory two levels *outside* `lm_dir`
(`<tmp>/escaped`) and reported `Linked ../../escaped` —
`created OUTSIDE lm_dir? True`.

**Recommended change**
- Validate the derived id before use: require exactly `owner/name`, forbid
  `.`/`..` components and path separators, and reject a resolved target that
  is not inside `lm_dir` (`Path(target).resolve().is_relative_to(lm_dir)` on
  3.9+, or an `os.path.commonpath` check).
- Treat an invalid id as unclassifiable/skipped with a warning.
- Add a regression test (CWE-22).

### C4 (Medium, correctness) — Ref content is not stripped; a trailing newline silently drops models

**Location:** `src/hf_lmstudio/cli.py:316-330`.

```python
with open(ref_file, encoding="utf-8") as f:
    ref = f.read()
...
snapshot_path = snapshots_dir / ref
if not snapshot_path.is_dir():
    _warn(f"Skipping {model_name}: snapshot no longer in cache")
    continue
```

`ref` is used verbatim, including any trailing whitespace/newline. The
resulting path (`snapshots/<hash>\n`) is not a directory, so the model is
reported as removed and skipped.

**Evidence (probe 5):** a cache entry whose `refs/main` contains `"aaa\n"`
yields `discovered: []` and the misleading warning
`Skipping org/alpha: snapshot no longer in cache`.

**Recommended change**
- `ref = f.read().strip()`, and validate the result (non-empty; optionally
  `len(ref) == 40` / hex, or `realpath(snapshot).is_relative_to(model_dir)`).
- Distinguish "unreadable ref" from "malformed ref" in the warning.
- Regression test with a newline-terminated ref.

### C5 (Medium, correctness/determinism) — Snapshot selection is `glob`-order dependent; `main` is not preferred

**Location:** `src/hf_lmstudio/cli.py:311-338`.

`refs_dir.glob("*")` returns an unspecified order, and the loop `break`s on
the first ref that resolves. Which ref wins is therefore non-deterministic,
and the result may be a PR or tag snapshot rather than `main`.

**Evidence (probe 6):** with `refs/main` and `refs/pr-42`, discovery chose
`prhash` — `chosen snapshot: prhash (expected mainhash)`.

**Recommended change**
- Iterate deterministically: prefer `"main"`, then other refs in sorted
  order; document the precedence (SPEC §2.3 only says refs point at
  snapshots).
- Regression test asserting `main` wins when present.

### C6 (Medium, correctness/UX) — Interactive selector crashes on an empty cache

**Location:** `src/hf_lmstudio/cli.py:98-145` (`select_models`) reached from
`manage_models` at `272-279`.

`manage_models` prints "No models found in Hugging Face cache" but still
calls `select_desired()` → `select_models([])`. With an empty list, `idx = 0`
and pressing SPACE does `selected[0] = not selected[0]` → `IndexError`; ↓
computes `min(-1, 1) = -1` and a later SPACE hits `selected[-1]` on an empty
list.

**Evidence (probe 1):** `CRASH: IndexError list index out of range`.

**Recommended change**
- Short-circuit before opening the UI when `model_choices` is empty (return
  `[]`), and guard the toggle/navigation when the list is empty.
- Add a test for the empty-cache path.

### C7 (Medium, design) — `select_models()` calls `sys.exit(0)` on cancel

**Location:** `src/hf_lmstudio/cli.py:137-139`.

```python
elif key == KEY_CTRL_C:
    print("\nImport is cancelled. Do nothing.")
    sys.exit(0)
```

A library-level selection function kills the process, which makes it hard to
test, prevents callers from cleaning up, and returns **success** (`0`) when
the user actually cancelled — contrary to clig.dev ("return zero exit code on
success, non-zero on failure"; SIGINT conventionally maps to `130`).

**Recommended change**
- Raise a small domain exception (e.g. `class Cancelled(Exception)`) or
  return a sentinel; map it to a non-zero exit code in `main()`.
- Print the cancellation notice to `stderr` as a message
  (`stderr` is for messaging).

### C8 (Low, robustness) — `_fetch_model_info` treats any `TypeError` as a version mismatch

**Location:** `src/hf_lmstudio/cli.py:58-67`.

Catching bare `TypeError` around `api.model_info(repo_id, timeout=...)` will
also catch a genuine `TypeError` raised *inside* the call, then call
`model_info` a second time (double request, possibly masking a bug).

**Recommended change**
- Narrow to the version-signature case, e.g. inspect the callable once with
  `inspect.signature`, or probe support on the client at first use and cache
  the decision, rather than retrying on an arbitrary `TypeError`.

### C9 (Low, correctness/drift) — Offline detection reimplements `huggingface_hub`'s

**Location:** `src/hf_lmstudio/cli.py:70-73`.

`_hub_offline()` invents its own truthiness list
(`"" , "0", "false", "no"`). `huggingface_hub` already exposes the canonical
flag as `huggingface_hub.constants.HF_HUB_OFFLINE`.

**Recommended change**
- When `huggingface_hub` is importable, use its constant; keep the local
  fallback only for the lazy-import case, and document the divergence.

### C10 (Low, design/testability) — Process-global mutable `HfApi` singleton

**Location:** `src/hf_lmstudio/cli.py:37-55`.

The shared client is module-global (`_hf_api`), which is why the tests must
patch `cli._get_hf_api` at module scope and why the online/offline state is
sticky within a process. It couples discovery/classification to global state.

**Recommended change**
- Pass the client (or a factory) into `get_model_type()` /
  `discover_models()`; default to the singleton in `main()`. This keeps the
  lazy-import rule (SPEC §4) while making tests take a real dependency
  instead of a monkeypatch.

### C11 (Low, correctness) — Settings file opened without an explicit encoding

**Location:** `src/hf_lmstudio/cli.py:259-262`.

`open(settings_path)` uses the platform default encoding. On non-UTF-8
locales a valid `settings.json` can fail to parse and silently fall back to
the default models directory.

**Recommended change**
- `open(settings_path, encoding="utf-8")`; catch `UnicodeDecodeError` as well
  (it is a `ValueError`, not an `OSError`).

### C12 (Low, UX) — Missing configured downloads folder is silently created

**Location:** `src/hf_lmstudio/cli.py:434` (`target_path.mkdir(parents=True, exist_ok=True)`).

If `downloadsFolder` points at a typo'd path, the tool creates the whole
directory tree rather than warning. clig.dev: "Actions crossing the boundary
of the program's internal world should usually be explicit."

**Recommended change**
- If `lm_dir` does not exist, warn (or prompt, when interactive) before
  creating it; keep `--all` non-interactive by warning only.

### C13 (Low, portability) — Absolute symlink targets

**Location:** `src/hf_lmstudio/cli.py:417` (`os.symlink(item, link_path)`).

`item` is an absolute path into the HF cache. If `HF_HOME`/the cache moves,
every created link dangles. The HF cache itself uses relative targets
(`docs/huggingface-cache.md` §snapshots).

**Recommended change**
- Create relative targets (`os.path.relpath(item, start=link_path.parent)`),
  or document why absolute targets are required and rely on the existing
  dangling-link cleanup.

### C14 (Low, correctness) — `is_fully_linked()` has vacuous-true and extra-entry blind spots

**Location:** `src/hf_lmstudio/cli.py:342-365`.

- A snapshot directory with **zero** entries returns `True` (the loop body
  never runs), so an empty model counts as "already imported".
- Extra files in the target (e.g. left over from a previous snapshot) are not
  detected, so the UI can show a stale link as fully correct.

**Recommended change**
- Require at least one snapshot entry, and optionally flag extra entries
  (or document that supersets are acceptable).

---

## 4. Findings — tests

### T1 (Medium) — No coverage for the destructive paths found here

There is no test for: the empty-cache UI path (C6), empty-directory pruning
of unrelated dirs (C1), preservation of a user's external symlink (C2),
path-traversal ids (C3), newline refs (C4), or `main` preference (C5). Every
C-finding above is therefore unguarded against regression. Add the targeted
tests listed in each finding.

### T2 (Low) — `tempfile.mkdtemp()` leaks directories

`tests/test_cli.py` calls `tempfile.mkdtemp()` in most tests without cleanup
(e.g. lines 81, 166, 267, 333, 362). Prefer `tempfile.TemporaryDirectory()`
with `self.addCleanup`, or a shared helper, to avoid leaking under `$TMPDIR`
and to keep tests hermetic.

### T3 (Low) — Permission-based test is unreliable

`test_unreadable_ref_skipped` (lines 124-139) relies on `chmod(0o000)`.
That is a no-op for `root` (as used in some CI containers) and behaves
differently on some filesystems, so the test can silently stop exercising the
error branch. Prefer `mock.patch("builtins.open", side_effect=OSError)` or
patching `Path.open`.

### T4 (Low) — Inconsistent assertion style

Assertions mix string values and the enum, and mix attribute and index
access: `assertEqual(cli.get_model_type(...), "GGUF")` vs
`cli.ModelFormat.OTHER`; `found[0][0]` / `found[0][1]` (line 150-151) vs
`found[0].model_name` (line 95-96). Compare against `ModelFormat` and
attribute access everywhere for clarity and refactor-safety.

### T5 (Low) — Module-level patcher is fragile

`setUpModule`/`tearDownModule` (lines 22-30) start a module-global patcher
and stash it in a global. A failure to start leaves later tests affected; it
also does not reset `cli._hf_api`. Prefer a `unittest.mock.patch` context per
test (or a base `TestCase` with `setUp`/`addCleanup`).

### T6 (Low) — Untested seams

No coverage for `main()`/argparse (including the non-TTY `parser.error` path),
`_hub_offline()` variants, `get_key()`, `manage_models()` directory
resolution, or `cleanup_dangling_links()` with nested directories. These are
small, pure-ish seams and cheap to test.

---

## 5. Findings — tooling and packaging

### G1 (Medium, packaging) — `license` and `classifiers` are misplaced and invalid

**Location:** `pyproject.toml:13-24`.

```toml
[project.optional-dependencies]
dev = ["ruff==0.17.1"]
license = { text = "MIT" }
classifiers = [ ... ]
```

Parsed metadata confirms they become **optional-dependency entries**, not
project metadata:

```
project keys: [... no license, no classifiers ...]
optional-dependencies: {"dev": [...], "license": {"text": "MIT"}, "classifiers": [ ... ]}
```

Two problems: (1) PEP 621 requires `optional-dependencies` values to be
arrays of strings, so `license = { text = "MIT" }` is invalid metadata that
will make a PEP 517 build fail; (2) `classifiers` becomes an extra named
`classifiers`. The distribution therefore advertises neither its licence nor
its classifiers (and `readme` is missing too).

**Recommended change**

```toml
[project]
# ...
license = { text = "MIT" }        # or SPDX: license = "MIT" (PEP 639)
readme = "README.md"
classifiers = [ ... ]

[project.optional-dependencies]
dev = ["ruff==0.17.1"]
```

and add a CI step that builds the distribution (e.g. `python -m build`) so
metadata errors are caught.

### G2 (Low) — Ruff config under-selects useful rules

`[tool.ruff.lint]` selects `E, F, B, BLE, I, UP, PLR` but omits families that
directly target the issues above: `PTH` (pathlib), `SIM`, `TRY`, `S`
(flake8-bandit, incl. `S108`/link issues) and `RUF`. Also, `ignore = ["E501"]`
neutered by `line-length = 100` makes the latter moot (already noted as N7).

**Recommended change**
- Add `PTH`, `SIM`, `TRY`, `RUF`, and `S` (with targeted ignores where
  genuinely needed), and add `ruff format --check` to CI.
- Consider `[tool.ruff.lint.per-file-ignores]` for tests rather than a global
  ignore.

### G3 (Low) — No `py.typed` marker (PEP 561)

The package ships type annotations but no `py.typed`, so installed
consumers/editors treat it as untyped.

**Recommended change**
- Add `src/hf_lmstudio/py.typed` and include it in the package data.

### G4 (Low) — CI does not type-check or build

`ruff` (style) and `unittest` (behaviour) run, but nothing verifies the
annotations (C10/C14-style errors are possible) and nothing builds the
distribution (which would have caught G1).

**Recommended change**
- Add a `mypy` (or `pyright`) job in non-strict mode, and a build job
  (`python -m build` / `pip wheel .`) to the workflow. Consider pinning
  GitHub Action versions by commit SHA for supply-chain hygiene.

### G5 (Low) — Version is not discoverable

`pyproject.toml` declares `version = "0.0.1"` but the CLI exposes no
`--version`, and 12-factor/CLI guidance says the version should be trivially
obtainable.

**Recommended change**
- Add `--version` to the parser (using `importlib.metadata.version`), and
  keep it consistent with a `CHANGELOG`/tag.

---

## 6. Prioritised list of recommended changes

| # | Prior. | Change | Where |
|---|--------|--------|-------|
| C2 | **High** | Only unlink symlinks the tool created / that resolve into the HF cache; preserve user symlinks | `cli.py:440-456`, `420-437` |
| C1 | **High** | Prune only directories that held a link we removed, never unrelated empty dirs | `cli.py:510-518` |
| G1 | **Med** | Move `license`/`classifiers`/`readme` into `[project]`; drop the invalid `optional-dependencies.license` | `pyproject.toml:13-24` |
| C3 | **Med** | Validate `model_name`; refuse `..`/absolute/separator ids and targets outside `lm_dir` (CWE-22) | `cli.py:304,350,422,442` |
| C6 | **Med** | Guard/forgo the interactive UI when no models are discovered | `cli.py:98-145,272-279` |
| C5 | **Med** | Deterministic ref order, prefer `main` | `cli.py:311-338` |
| C4 | **Med** | `strip()` and validate ref content; distinguish malformed from missing | `cli.py:316-330` |
| C7 | **Med** | Replace `sys.exit(0)` in `select_models` with a domain exception; exit non-zero on cancel | `cli.py:137-139` |
| T1 | **Med** | Add regression tests for C1–C6 | `tests/test_cli.py` |
| G1/G4 | **Med** | Add a build/type-check step to CI | `.github/workflows/test.yml` |
| C14 | Low | Require a non-empty snapshot; handle extra target entries | `cli.py:342-365` |
| C8 | Low | Narrow the `TypeError` fallback in `_fetch_model_info` | `cli.py:58-67` |
| C9 | Low | Prefer `huggingface_hub.constants.HF_HUB_OFFLINE` | `cli.py:70-73` |
| C10 | Low | Inject the `HfApi` client instead of a global | `cli.py:37-55` |
| C11 | Low | Open `settings.json` with `encoding="utf-8"`; catch `UnicodeDecodeError` | `cli.py:259-270` |
| C12 | Low | Warn before creating a missing downloads folder | `cli.py:434` |
| C13 | Low | Prefer relative symlink targets | `cli.py:417` |
| T2–T6 | Low | Test hygiene: `TemporaryDirectory`, mock-based permission test, consistent asserts, per-test patcher, cover `main`/`get_key`/`manage_models` | `tests/test_cli.py` |
| G2 | Low | Enable `PTH`/`SIM`/`TRY`/`RUF`/`S`; add `ruff format --check` | `pyproject.toml:35-37` |
| G3 | Low | Add `py.typed` | `src/hf_lmstudio/` |
| G5 | Low | Add `--version` | `cli.py:521-539` |

Suggested order: **C2, C1, G1** first (data-loss / broken metadata), then
**C3, C6, C5, C4, C7** (security and silent-drop bugs), then the tests
(**T1**) alongside each fix, then the Low items.

---

## 7. Verification appendix

Environment: macOS, Python 3.15.0; tree = `code_review` working copy.
`python -m unittest discover -s tests` → `Ran 39 tests … OK`.
`ruff` was not installed locally, so lint was not re-run here (the
`code_review` branch previously passed `ruff check .`).

Probe script (summarised; all run against `src/` in a temporary sandbox):

```text
Probe 1  select_models([]) with SPACE      -> CRASH: IndexError list index out of range
Probe 2  cleanup_dangling_links on a      -> user empty dir still exists? False
         user-created empty dir
Probe 3  _unlink_model on a user symlink  -> Unlinked org/model-a
         pointing outside hub                 user symlink still exists? False
Probe 4  nested GGUF snapshot linking     -> linked entries: ['Q4_K_M','config.json']
                                              fully linked? True
Probe 5  refs/main containing "aaa\n"     -> discovered: []
                                              "Skipping org/alpha: snapshot no longer in cache"
Probe 6  refs/main + refs/pr-42           -> chosen snapshot: prhash (expected mainhash)
Probe 7  _link_model over a user          -> target now a dir of symlinks? True
         symlink-to-dir
Probe 8  cache dir models--..--..--escaped-> discovered model_name = '../../escaped'
                                              created OUTSIDE lm_dir? True
```

Packaging check:

```text
tomllib parse of pyproject.toml
project keys: [authors, dependencies, description, name, optional-dependencies,
               requires-python, scripts, urls, version]      # no license/classifiers/readme
optional-dependencies: {"dev": [...], "license": {"text": "MIT"}, "classifiers": [...]}
```

Standards consulted are listed in §1; `docs/code_review.md` remains the record
of round 1 (findings F1–F10, R1–R9, N1–N8) and is not superseded, only
extended, by this document.
