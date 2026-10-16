# Design and Architecture: hf-lmstudio

(written by gemma-4-12b-it-optiq)

## Overview

`hf-lmstudio` is a CLI extension for the Hugging Face (`hf`) toolset. Its primary purpose is to bridge the gap between models downloaded via the Hugging Face Hub and those used by LM Studio (or Bionic).

By creating symbolic links, the tool allows users to share model files between these environments without duplicating storage space. The user's selection defines the desired state: selected models end up linked into LM Studio, unselected models end up unlinked, and no existing user files are ever modified or deleted — only symlinks are created or removed (see `docs/SPEC.md` §2).

## Architecture

The software follows a simple, procedural CLI architecture designed for interactive user input and filesystem manipulation. Logic is split into separable functions so each part can be tested without a terminal.

### Components

1. **CLI Entry Point (`cli.py`)**:
   - `main()` handles argument parsing (`--all`/`-a`) and delegates to `manage_models()`.
   - `manage_models()` resolves the HF Hub cache dir and the LM Studio downloads dir, then runs discovery, selection, reconciliation, and cleanup in sequence.

2. **Model Discovery Engine (`discover_models`)**:
   - Scans the Hugging Face Hub cache directory (determined by `HF_HUB_CACHE`, `HF_HOME`, or the default `~/.cache/huggingface/hub`).
   - Identifies model snapshots by parsing the `refs` directory under each `models--*` folder.
   - Hardened against unusual cache layout: skips non-file entries in `refs/` (e.g. `refs/refs/pr/1`), reports and skips unreadable refs, and skips models whose snapshot is no longer in the cache.
   - Deduplicates discovered models by model name so multiple refs for one repo yield a single entry.

3. **Model Classification (`get_model_type`)**:
   - Multi-tier classification (SPEC §2.4):
     1. Local GGUF check: any `.gguf` file in the snapshot directory → `GGUF`.
     2. Organisation check: repo ID starting with `mlx-community/` → `MLX`.
     3. Hub metadata via `huggingface_hub.HfApi().model_info()` (imported lazily at runtime, not a hard dependency): `library == "gguf"`/`mlx` or matching tags → `GGUF`/`MLX`.
     4. `config.json` `model_type` fallback (lowercased).
     5. `Unknown` when nothing applies; such models are reported and skipped, never linked.

4. **Interactive UI (`select_models`, `get_key`, `select_desired`)**:
   - A terminal-based selection menu (arrow keys to move, Space to toggle, Enter to confirm, Ctrl+C to cancel) using POSIX raw mode via `termios`/`tty`.
   - `select_desired()` returns the set of model names the user wants linked. With `--all`, every classifiable model is selected without a terminal (SPEC §2.2); otherwise the interactive menu defines desired state (SPEC §2.6).
   - Models already imported are annotated "(already imported)" based on whether a symlink exists at the target.

5. **Filesystem Manager (`apply_desired_state`, `cleanup_dangling_links`)**:
   - `apply_desired_state()` makes the LM Studio dir match the desired state (SPEC §2.5, §2.6):
     - Selected models are linked as a directory of symlinks into the HF cache snapshot. Existing symlinks we created are replaced; real files or directories at link targets are left untouched and reported.
     - Unselected models have any link we created removed. User files inside a previously-linked directory are kept; only our symlink entries are dropped, and the directory is removed if empty.
   - `cleanup_dangling_links()` removes dangling symlinks that point into the HF cache (covering models removed from the cache) and prunes now-empty directories, never removing the LM Studio dir itself (SPEC §2.6, §6.3).

## Data Flow

1. **Resolution**: Resolve the HF Hub cache dir and the LM Studio downloads dir (from `~/.lmstudio/settings.json` `downloadsFolder`, or `~/.lmstudio/models`).
2. **Discovery**: Scan `models--*` cache folders, parse `refs/` into `snapshots/` paths, harden against odd layout, dedupe by model name.
3. **Classification**: Run `get_model_type()` on each snapshot; report and skip `Unknown` models.
4. **Presentation**: Display the list with type and "(already imported)" status.
5. **Selection**: User picks models via the interactive menu, or `--all` selects all classifiable models → defines the desired state.
6. **Reconciliation**: Apply desired state — link selected models, unlink unselected ones, never overwrite real user files.
7. **Cleanup**: Remove dangling symlinks pointing into the HF cache; prune empty directories left behind.

## Technical Details

- **Language**: Python 3.10+
- **Key Libraries**: `pathlib`, `json`, `glob`, `argparse`, `os`, `sys`, `tty`/`termios` (POSIX only, for raw terminal input).
- **Runtime dependency note**: `huggingface_hub` is imported lazily inside `get_model_type()` and is provided by the `hf` CLI runtime under `uvx`; it is not a hard install-time dependency (SPEC §4).
- **Symlinking**: Uses `os.symlink` to ensure the actual model weights remain in the Hugging Face cache while appearing as local files in LM Studio.

## Known Limitations & Future Work

- **Terminal handling**: Raw-mode key input relies on POSIX `termios`; Windows is unsupported.
- `docs/Design.md` and `docs/model_type_changes.md` remain for historical context per SPEC §5; this document reflects the current implementation (steps 1–8 of `docs/changes.md`).
