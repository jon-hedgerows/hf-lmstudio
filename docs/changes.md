# Change Plan: hf-lmstudio implementation → docs/SPEC.md conformance

## Purpose

This document plans the changes needed to bring `src/hf_lmstudio/cli.py` into conformance with `docs/SPEC.md` (the source of truth for intended behaviour). Changes are broken into independent steps; each step can be implemented and verified on its own, in the order listed (or out of order where noted).

## 0. Context & ground rules

- **Source of truth:** `docs/SPEC.md`. Do not edit `SPEC.md` as part of this plan; mark spec clauses satisfied here instead.
- user-facing install/usage lives in the root `README.md`, which is already consistent with SPEC §2.1–2.2 (`hf extensions install jon-hedgerows/hf-lmstudio`, `hf lmstudio`, `hf lmstudio --all`).
- Source lives in `src/hf_lmstudio/cli.py`; tests go in `tests/` (stdlib `unittest` only — no new dependencies, per SPEC §4).
- **Runtime dependency rule (SPEC §4):** standard library only at install time. `huggingface_hub` is provided by the `hf` command under `uvx`, so it may be imported _lazily_ inside functions (wrapped in `try/except ImportError`), but `pyproject.toml` must not gain a hard dependency.
- **Safety rule (SPEC §2.5–2.6):** never modify or delete existing user files. Only symlinks may be created or removed. Real directories/files at link targets are left untouched and reported.
- `Design.md` and `model_type_changes.md` remain for historical context per SPEC §5.
- Known limitations in SPEC §6 addressed by this plan: 1 (`--all`), 2 (model type accuracy), 3 (symlink bookkeeping / links for cache-removed models). Limitation 4 (POSIX terminal, Windows unsupported) remains documented; no change.

## Gap summary (SPEC vs current code)

| SPEC clause                | Required                                                            | Current code                                                                                                    | Gap                           |
| -------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| §2.2 `hf lmstudio --all`   | Link all compatible models, non-interactive                         | Flag parsed, but `--all` branch unpacks 4 values from 3-tuples (`ValueError`) and removes already-linked models | Not implemented; crashes      |
| §2.6 Desired state         | Selected → linked; unselected → unlinked                            | Toggles: imported-selected removed, non-imported-added; unselected models never touched                         | Inverted and incomplete       |
| §2.5 Relink replaces       | Replace existing dir/symlink                                        | `os.symlink` crashes if target exists; `shutil.rmtree` deletes real directories                                 | Crash risk; deletes user data |
| §2.4 Skip unknown          | Report and skip unclassifiable models                               | `Unknown` models are added and linked                                                                           | Not skipped                   |
| §2.6 Dangling links        | Remove dangling symlinks → HF cache; remove empty dirs              | Not done                                                                                                        | Missing                       |
| §2.4 / §6.2 Classification | GGUF / MLX / other via config fallback; Hub API more accurate       | `config.json` `model_type` only; CLIP mislabelled                                                               | Inaccurate                    |
| §6.3 Bookkeeping           | Desired state from selection; remove links for cache-removed models | Reconciled against disk only                                                                                    | Missing                       |
| §2.3 Discovery             | `models--*`, `refs` → `snapshots`                                   | `refs/` subdirectories crash `open()`                                                                           | Fragile                       |
| §5 Tests                   | Tests in `tests/`                                                   | None                                                                                                            | Missing                       |

## Step 1 — Skip unclassifiable models (SPEC §2.4)

**Goal:** Models that cannot be classified are reported and skipped, never linked.

**Changes (`src/hf_lmstudio/cli.py`):**

- In the discovery loop in `manage_models`, after `get_model_type(...)`, if the type is `"Unknown"`, print a skip message (e.g. `Skipping <model>: could not classify`) and `continue` — do not add it to `found_models`.
- Classifiable types remain: `GGUF`, `MLX`, and any non-empty `model_type` from `config.json`.

**Verification (independent):**

- Build a temp HF cache containing one model with no `config.json`, no `.gguf` file, and a repo id not under `mlx-community/`.
- Run `manage_models` (or call the discovery piece) and confirm: the model is reported as skipped, is absent from the discovered list, and nothing is linked for it.

## Step 2 — Harden model discovery (SPEC §2.3)

**Goal:** Discovery never crashes on unusual cache layout.

**Changes (`src/hf_lmstudio/cli.py`):**

- When iterating `refs_dir/*`, skip entries that are not regular files (e.g. `refs/refs/pr/1` subdirectories described in `docs/huggingface-cache.md`).
- Wrap ref-file reading in `try/except`; on error, report and skip that ref rather than crashing.
- Dedupe discovered models by model name (first snapshot wins) so multiple refs for one repo don't collide on the same link target.

**Verification (independent):**

- Temp cache with a `models--*` repo whose `refs/` contains a subdirectory and one unreadable ref file → discovery completes without crashing and yields the readable refs.
- Two refs pointing to different snapshots of the same repo → one discovered entry, not two.

## Step 3 — Desired-state reconciliation (SPEC §2.5, §2.6)

**Goal:** The user's selection defines desired state: selected models end up linked, unselected models end up unlinked. Relinking replaces symlinks without touching real files. This is the core fix; it also removes the inverted-logic bug.

**Changes (`src/hf_lmstudio/cli.py`):**

- Refactor `manage_models` into separable pieces so each can be tested without a terminal:
  - `discover_models(hub_dir) -> list[(model_type, model_name, snapshot_path)]` (Steps 1–2 logic).
  - `select_desired(models, autolink_all) -> set[model_name]` — interactive via existing `select_models` when not `--all`; all classifiable names when `--all`.
  - `apply_desired_state(lm_dir, discovered, desired) -> None` — pure filesystem reconciliation (below).
- `apply_desired_state` logic for each discovered model (`target = lm_dir / model_name`):
  - **Selected:**
    - If `target` exists and is **not** a symlink → report "skipped: real file/directory", do nothing (never modify user files).
    - If `target` is a symlink → `unlink()` it (replace).
    - `target.mkdir(parents=True, exist_ok=True)`; for each item in the snapshot dir: if `target/item.name` exists (symlink), unlink it first, then `os.symlink(item, link_path)`. Report linked.
  - **Not selected:**
    - If `target` is a symlink (dangling or not) → `unlink()`. Report unlinked.
    - Real files/dirs → left alone.
- Keep the existing "(already imported)" annotation in the interactive menu; it reflects `target.is_symlink()`.

**Verification (independent):**

- Sandbox cache + LM Studio dir. Run reconciliation with a fixed desired set (bypass the menu via the refactored function).
- Confirm: selected models → directory of symlinks; unselected but previously linked → symlink removed; unselected and never linked → nothing; real directory pre-existing at a target → untouched; running twice with the same selection → no `FileExistsError`, idempotent.

## Step 4 — Implement `--all` (SPEC §2.2, §6.1)

**Goal:** `hf lmstudio --all` links every compatible model without interactive selection and never crashes.

**Changes (`src/hf_lmstudio/cli.py`):**

- Wire `--all` through `select_desired(models, autolink_all=True)` → all discovered classifiable model names; bypass `select_models` entirely (no terminal required).
- Delete the broken 4-tuple unpack in the old `--all` branch.
- Print summary (e.g. `Linking N models...` / `Linked N models`).

**Verification (independent):**

- Temp cache with ≥2 classifiable models. Run `python -m hf_lmstudio --all` (or `manage_models(autolink_all=True)`) with no TTY.
- Confirm: all models linked, no interactive prompts, no crash; second run is idempotent.

## Step 5 — Dangling-symlink and empty-directory cleanup (SPEC §2.6, §6.3)

**Goal:** After reconciliation, dangling symlinks in the LM Studio dir that point into the HF cache are removed; empty directories left behind are removed. This covers links for models removed from the cache (their snapshots no longer exist → dangling).

**Changes (`src/hf_lmstudio/cli.py`):**

- Add `cleanup_dangling_links(lm_dir, hub_dir)` called at the end of `manage_models`:
  - Walk `lm_dir` bottom-up. For each symlink: if `os.path.exists(link)` is False (dangling) and `os.readlink(target)` resolves to a path under `hub_dir` → `unlink()`.
  - Then remove directories that are now empty (bottom-up), never removing `lm_dir` itself.
- Only symlinks pointing into `hub_dir` are candidates; other dangling links are left alone.

**Verification (independent):**

- Pre-create in the LM Studio dir: (a) a dangling symlink pointing into the HF cache, (b) an empty subdirectory, (c) a valid symlink and a real file as controls.
- Run the tool; confirm (a) and (b) removed, (c) untouched.

## Step 6 — Model type accuracy via Hub API (SPEC §2.4, §6.2)

**Goal:** Classification uses local checks first, then Hub metadata (library/tags), then `config.json` fallback — reducing misclassification (e.g. CLIP no longer labelled MLX).

**Changes (`src/hf_lmstudio/cli.py`):**

- Rewrite `get_model_type(snapshot_path, repo_id)`:
  1. If snapshot dir contains any `.gguf` file → `"GGUF"`.
  2. If `repo_id.startswith("mlx-community/")` → `"MLX"`.
  3. Lazily `from huggingface_hub import HfApi` (try/except `ImportError`/exception → fall through):
     - `info.library == "gguf"` or `"gguf"` in `info.tags` → `"GGUF"`.
     - `info.library == "mlx"` or `"mlx"` in `info.tags` → `"MLX"`.
  4. If `config.json` exists → its `model_type` (lowercased), or `"Unknown"` if absent/unreadable.
- Return `"Unknown"` only when all tiers fail; callers skip it (Step 1).
- No change to `pyproject.toml` (SPEC §4: `huggingface_hub` is provided by the `hf` runtime).

**Verification (independent):**

- Unit-style checks of `get_model_type` against fake snapshots:
  - `.gguf` present → `GGUF` (even with misleading config/tags).
  - `mlx-community/` repo id → `MLX`.
  - Fake `HfApi` returning `library="gguf"` / tag `"gguf"` → `GGUF`; `library="mlx"` / tag `"mlx"` → `MLX`.
  - `config.json` `{"model_type": "llama"}` → `"llama"`; nothing identifiable → `"Unknown"`.
  - `HfApi` import failure or network error → falls back to config / `Unknown`, no crash.

## Step 7 — Tests (SPEC §5)

**Goal:** Add test coverage so future changes can be verified without manual sandbox runs.

**Changes:**

- Add `tests/test_cli.py` using stdlib `unittest` + `tempfile.TemporaryDirectory`:
  - `discover_models`: model dir layout, refs→snapshots resolution, subdir refs skipped, dedupe by name.
  - `get_model_type`: all tiers from Step 6 (with faked `huggingface_hub`).
  - `apply_desired_state`: link selected, unlink unselected, skip real files, idempotent relink.
  - `cleanup_dangling_links`: dangling→cache removed, empty dirs pruned, controls untouched.
- Update `tests/README.md` to record that tests now exist and how to run them.

**Verification (independent):**

- `python -m unittest discover -s tests` passes (or `python -m pytest tests` if pytest is available).

## Step 8 — Closeout (end-to-end verification)

**Goal:** Confirm the whole flow matches SPEC §2.

- Run Steps 1–7 verifications in one sandbox session: interactive run (menu renders, arrow/Space/Enter/Ctrl+C per SPEC §2.7 — UI already matches; no code change expected), `--all` run, deselect run, dangling-cleanup run.
- Mark each step complete in this document.
- `docs/Design.md` and `docs/model_type_changes.md` remain as historical context (SPEC §5); no further doc edits required unless behaviour diverges from SPEC again.

## Further changes after spec clarification (`e1c95a1 clarify spec`)

The clarified SPEC (§2.6) adds UI initialization rules that my implementation
does not yet meet:

- A model shows in the UI as **selected** only when it is already *fully and
  correctly symlinked* in LM Studio.
- A model shows as **unselected** when it is not found in LM Studio, or when
  the symlinks are *incomplete*.
- Directories must not be removed unless they previously contained a symlink
  to the hf cache.
- No existing _files_ are ever modified; existing symlinks may be updated,
  created, or removed; directory structures should be updated, created, or
  removed as required.

The §6 "Known Limitations / Future Work" section was removed, so `--all`,
model-type accuracy, and symlink bookkeeping are now expected behaviour, not
limitations.

### Step 9 — Fix UI initialization: "fully and correctly symlinked" (SPEC §2.6)

**Goal:** The UI must show a model as selected only when every snapshot file
is present as a correct symlink in LM Studio; incomplete or missing links show
as unselected.

**Problem:** `select_desired` currently uses `target_path.is_symlink()`, but
linked targets are directories containing symlinks, not symlinks themselves.
So a fully linked model would show as unselected in the UI.

**Changes (`src/hf_lmstudio/cli.py`):**
- Add `is_fully_linked(lm_dir, model_name, snapshot_path) -> bool`:
  - Returns `False` if target does not exist or is not a directory.
  - Returns `False` if any snapshot file is missing from the target, or if a
    target entry exists but is not a symlink pointing at the corresponding
    snapshot file.
  - Returns `True` only when all snapshot files are present as correct symlinks.
- In `select_desired`, replace `target_path.is_symlink()` with
  `is_fully_linked(lm_dir, model_name, snapshot_path)` to compute the
  "(already imported)" status and initial selection state.

**Verification (independent):**
- Temp cache + LM dir with a fully linked model → shows as selected in UI.
- Temp cache + LM dir with a partially linked model (missing one symlink) →
  shows as unselected in UI.
- Temp cache + LM dir with no link → shows as unselected.

### Step 10 — Fix `apply_desired_state` to complete partial links (SPEC §2.5, §2.6)

**Goal:** When a model is selected, any missing or incorrect symlinks are
created/replaced without touching real user files. When unselected, only our
own symlinks are removed; directories are removed only if empty (i.e. they
contained only our symlinks).

**Changes (`src/hf_lmstudio/cli.py`):**
- For selected models: instead of skipping when the target directory exists,
  iterate the snapshot files:
  - If a target entry is a correct symlink → leave it.
  - If a target entry is a wrong symlink → replace it.
  - If a target entry is a real file/directory → skip it, report it (never
    modify user files).
  - If a target entry is missing → create the symlink.
- For unselected models: keep current behaviour (remove our symlinks, then
  `rmdir` if empty — this already satisfies "directories must not be removed
  unless they previously contained a symlink to the hf cache").
- If the target is a real file (not a directory) → skip, report.

**Verification (independent):**
- Partially linked model selected → missing symlinks created, existing ones
  kept, real user files untouched.
- Wrong symlink replaced with correct one.
- Unselected model with our symlinks + user files → symlinks removed, user
  files kept, directory kept if not empty.

### Step 11 — Update tests and final verification (SPEC §5, §2)

**Goal:** Test coverage for the clarified UI initialization and partial-link
completion behaviour.

**Changes:**
- Add tests for `is_fully_linked`: fully linked, partially linked, missing
  target, real file target, wrong symlink target.
- Update `select_desired` tests to use `is_fully_linked`-based expectations.
- Add tests for completing partial links in `apply_desired_state`.
- Re-run `python -m unittest discover -s tests` — all must pass.

**Verification (independent):**
- End-to-end sandbox: link a model, re-run with `--all`, confirm it shows as
  already linked (selected) and no changes are made; unlink it, confirm it
  shows as unselected.

## Completion (updated)

## Completion (updated)

All steps are implemented and verified, each in its own commit on the
`spec-updates` branch:

| Step | Summary | SPEC clauses | Status |
|------|---------|--------------|--------|
| 1 | Skip unclassifiable models | §2.4 | Done |
| 2 | Harden model discovery | §2.3 | Done |
| 3 | Desired-state reconciliation | §2.5, §2.6 | Done |
| 4 | Implement `--all` | §2.2, §6.1 | Done |
| 5 | Dangling-symlink / empty-dir cleanup | §2.6, §6.3 | Done |
| 6 | Model type accuracy via Hub API | §2.4, §6.2 | Done |
| 7 | Tests (stdlib `unittest`) | §5 | Done |
| 8 | End-to-end verification | §2 | Done |
| 9 | Fix UI initialization: "fully and correctly symlinked" | §2.6 | Done |
| 10 | Complete partial links in `apply_desired_state` | §2.5, §2.6 | Done |
| 11 | Update tests and final verification | §5, §2 | Done |

Verification performed:
- `python -m unittest discover -s tests` → 32 tests pass.
- Sandbox runs confirm: UI shows "already imported" only when fully and
  correctly symlinked; partial links are completed when selected; wrong
  symlinks are replaced; real user files are never modified; unselected
  models are unlinked with empty dirs pruned; `--all` is idempotent without a
  TTY; cache-removed models are pruned via dangling-link cleanup.

### Additional clarification addressed (after `e1c95a1 clarify spec`)

**UI pre-selection and "no changes on confirm" (SPEC §2.6):**
- `select_models()` now initializes `selected[]` from the `is_imported` flag
  in each `model_choices` entry, so fully and correctly symlinked models are
  pre-selected (◉) in the UI before the user sees them.
- Confirming with no changes calls `apply_desired_state()` with only the
  pre-selected models; correct symlinks are skipped, so nothing on disk
  changes. Covered by `TestNoChangeOnConfirm` and `TestSelectModels`.

Unchanged per SPEC: the interactive raw-mode UI (already matches §2.7),
POSIX-only terminal handling (§2.7 / line 11), and `Design.md` /
`model_type_changes.md` remain as explanatory material (§5).
