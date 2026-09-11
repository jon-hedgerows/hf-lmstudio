# Design and Architecture: hf-lmstudio

_(written by gemma-4-12b-it-optiq)_

## Overview

`hf-lmstudio` is a CLI extension for the Hugging Face (`hf`) toolset. Its primary purpose is to bridge the gap between models downloaded via the Hugging Face Hub and those used by LM Studio (or Bionic).

By creating symbolic links, the tool allows users to share model files between these environments without duplicating storage space.

## Architecture

The software follows a simple, procedural CLI architecture designed for interactive user input and filesystem manipulation.

### Components

1.  **CLI Entry Point (`cli.py`)**:
    - Handles the main execution flow.
    - Orchestrates model discovery, user selection, and symlink creation/removal.
2.  **Model Discovery Engine**:
    - Scans the Hugging Face Hub cache directory (determined by `HF_HUB_CACHE`, `HF_HOME`, or default paths).
    - Identifies model snapshots by parsing the `refs` directory.
    - Attempts to distinguish between MLX and GGUF models by checking for `config.json` files within the snapshot paths.
3.  **Interactive UI**:
    - A terminal-based selection menu that allows users to navigate through discovered models using arrow keys and select them with the spacebar.
    - Handles terminal raw mode to capture single keypresses for a responsive UI experience.
4.  **Filesystem Manager**:
    - Identifies the LM Studio downloads directory (reading from `~/.lmstudio/settings.json` or defaulting to `~/.lmstudio/models`).
    - Manages the creation of symbolic links from the HF cache to the LM Studio directory.
    - Handles cleanup by removing existing directories or links if a model is re-imported.

## Data Flow

1.  **Discovery**: Scan `~/.cache/huggingface/hub` -> Identify model snapshots.
2.  **Classification**: Check for `config.json` -> Label as MLX or GGUF.
3.  **Presentation**: Display list of models with "already imported" status to the user.
4.  **Selection**: User selects specific models via interactive menu.
5.  **Execution**:
    - If already imported: Remove existing path/link.
    - If new: Create directory structure and symlink all files from the snapshot path to the LM Studio target.

## Technical Details

- **Language**: Python 3.10+
- **Key Libraries**: `pathlib`, `json`, `shutil`, `glob`, `termios` (for raw terminal input).
- **Symlinking**: Uses `os.symlink` to ensure that the actual model weights remain in the Hugging Face cache while appearing as local files in LM Studio.

## Known Limitations & Future Work

- **Model Type Accuracy**: Currently, any model with a `config.json` is labeled as MLX. More granular checks are needed to distinguish between different architectures (e.g., CLIP vs. LLM).
- **Bulk Import**: Implementation of a `--all` flag to automatically link all compatible models.
