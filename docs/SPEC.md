# Specification: hf-lmstudio

Living documentation for the project's intended behaviour, architecture, and development workflow.

## 1. Purpose

`hf-lmstudio` is a [`hf` CLI extension](https://huggingface.co/docs/huggingface_hub/main/en/guides/cli-extensions) that links models in the Hugging Face cache to LM Studio's downloads folder.

By creating symbolic links, the tool lets LM Studio use models that were downloaded with `hf` or other Hub-compatible tools, without duplicating storage. This specification defines the project's intended behaviour, architecture, and development workflow.

This plugin only supports macos and linux, Windows is not supported.

## 2. User-Facing Behaviour

### 2.1 Installation

The extension is installed from GitHub into an existing `hf` installation:

```shell
hf extensions install jon-hedgerows/hf-lmstudio
```

The installed command is `hf lmstudio`.

### 2.2 Commands

| Command             | Behaviour                                                                                                            |
| ------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `hf lmstudio`       | Interactive: list models found in the Hub cache, let the user select which to link, and symlink them into LM Studio. |
| `hf lmstudio --all` | Link every compatible model without interactive selection.                                                           |

### 2.3 Model discovery

Models are discovered by scanning the Hugging Face Hub cache directory:

- The directory is resolved in order of precedence: `$HF_HUB_CACHE`, then `$HF_HOME/hub`, then `~/.cache/huggingface/hub`.
- The Hub cache layout groups model repos under directories matching `models--*`. Within these, snapshot references are found in the `refs` directory, pointing at paths under `snapshots/`.
- Full details of the cache layout are at [Hub Local Cache](https://huggingface.co/docs/hub/en/local-cache.md)

### 2.4 Model classification

Each discovered model is classified to decide where/how it may be linked:

- **GGUF**: the snapshot directory contains a `.gguf` file (searched recursively).
- **MLX**: the repository ID starts with `mlx-community/`.
- Hub metadata: queried via `huggingface_hub` `HfApi().model_info()`; `library_name` or tags containing `"gguf"`/`"mlx"` are used as additional signals. The call is skipped when `HF_HUB_OFFLINE` is set or the Hub is unreachable.
- Any other model falls back to reading `model_type` from `config.json`.
- Models that cannot be classified are reported as `"unknown"` and included in discovery for the user to decide whether to link.

### 2.5 Linking behaviour

- The target directory is LM Studio's downloads folder, resolved in order of precedence: `~/.lmstudio/settings.json` (`downloadsFolder`), then `~/.lmstudio/models`.
- Linking creates a directory of symlinks inside the target, one symlink per file in the model's snapshot directory.
- If a model is already linked, linking again replaces the existing directory or symlink.
- Symlinks are used deliberately: model weights remain in the HF cache and are not duplicated.

### 2.6 Desired state and the interactive selector

Running `hf lmstudio` presents an interactive selector, and the user's selection defines the desired state for every discovered model:

When initialising the list of models in the UI:

- where a model is already fully and correctly symlinked in LM Studio, the model shows in the UI as **selected**
- where a model is not found in LM Studio, or the symlinks are incomplete, the model shows in the UI as **unselected**

The User can then select or deselect individual models.

- **Selected** models must end up linked (symlinked) into LM Studio.
- **Unselected** models must end up _not_ linked — any existing link for them is removed.
- Dangling symlinks in the LM Studio models directory (links to models that are no longer in the hf cache) are removed if they point towards files in the HuggingFace cache. If this removal leaves a directory empty, then the directory is removed.
- directories must not be removed unless they previously contained a symlink to the hf cache.
- No existing _files_ are ever modified; existing symlinks may be updated, created, or removed; directory structures should be updated, created, or removed as required.

The existing links are reported when discovered (models already imported are annotated as such).

### 2.7 Interactive UI

The interactive selector renders a navigable menu (arrow keys to move, Space to toggle, Enter to confirm, Ctrl+C to cancel) using terminal raw mode. The user's final selection determines the desired state, as described in §2.6.

## 3. Architecture

The implementation is a single procedural module, `src/hf_lmstudio/cli.py`, with the following responsibilities:

- **CLI entry point** — argument parsing (`main()`), delegating to `manage_models()`.

- **Model discovery** — enumerate Hub cache directories, resolve snapshot references, build the model list.
- **Model classification** — `get_model_type()`, deciding MLX/GGUF/other.
- **Interactive UI** — `select_models()` and `get_key()`, rendering the selection menu and defining the desired state.
- **Filesystem management** — `manage_models()` locating LM Studio's folder and creating/removing symlinks to match the desired state.

The module is designed to run under `uvx` execution of the installed `hf` extension, so it must not depend on the project being installed locally.

## 4. Dependencies

- Python 3.10+ (per `pyproject.toml`).
- No third-party runtime dependencies: the `hf` CLI itself (from `huggingface_hub`) provides the command environment.
- Standard library only: `pathlib`, `json`, `shutil`, `glob`, `argparse`, `os`, `sys`, `tty`/`termios` (POSIX only).

## 5. Development Workflow

- Source lives in `src/hf_lmstudio/`; tests live in `tests/` (currently no tests are written — `tests/README.md` records this).
- Build artefacts (`build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `*.pyc`) and virtual environments (`.venv/`) are excluded from version control and should not be committed.
- Documentation for the design intent covered in `Design.md` is to be updated in line with the actual design in accordance with this specification. The design file remains in the repository as explanatory material.
