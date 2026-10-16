import json
import os
from pathlib import Path
import sys
import glob
import argparse

# credit: much of this was written by Ivan Fioravanti


def select_models(model_choices):
    selected = [False] * len(model_choices)
    idx = 0
    window_size = os.get_terminal_size().lines - 5

    while True:
        print("\033[H\033[J", end="")
        print(
            "❯ hf lmstudio - Hugging Face Model Link \nAvailable models (↑/↓ to navigate, SPACE to select, ENTER to confirm, Ctrl+C to quit):"
        )

        window_start = max(
            0, min(idx - window_size + 3, len(model_choices) - window_size)
        )
        window_end = min(window_start + window_size, len(model_choices))

        for i in range(window_start, window_end):
            display_name, _, _, _ = model_choices[i]
            print(
                f"{'>' if i == idx else ' '} {'◉' if selected[i] else '○'} {display_name}"
            )

        key = get_key()
        if key == "\x1b[A":  # Up arrow
            idx = max(0, idx - 1)
        elif key == "\x1b[B":  # Down arrow
            idx = min(len(model_choices) - 1, idx + 1)
        elif key == " ":
            selected[idx] = not selected[idx]
        elif key == "\r":  # Enter key
            break
        elif key == "\x03":  # Ctrl+C
            print("\nImport is cancelled. Do nothing.")
            sys.exit(0)

    return [
        choice for choice, is_selected in zip(model_choices, selected) if is_selected
    ]


def get_key():
    """Get a single keypress from the user."""
    import tty, termios

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def get_model_type(snapshot_path, repo_id):
    """
    Determines the model format
    """
    # 1. Local GGUF Check: Look for .gguf files in the snapshot directory
    if any(item.suffix == ".gguf" for item in Path(snapshot_path).iterdir()):
        return "GGUF"

    # 2. Organization Check: If it's in mlx-community, assume it's a MLX model
    if repo_id.startswith("mlx-community/"):
        return "MLX"

    # 3. Try reading from config.json
    if os.path.exists(os.path.join(snapshot_path, "config.json")):
        try:
            with open(os.path.join(snapshot_path, "config.json")) as f:
                config = json.load(f)
                model_type = config.get("model_type", "").lower()
                return model_type
        except (json.JSONDecodeError, FileNotFoundError):
            # failed to read an existing config.json, skip this model
            pass

    # 4. We don't know - at this point there may be some more information available
    # in the huggingface API
    return "Unknown"


def manage_models(autolink_all: bool = False):
    """Import models from the Hugging Face cache."""
    # the hub dir is set by $HF_HUB_CACHE if defined, or $HF_HOME/hub if defined, or ~/.cache/huggingface/hub
    hub_dir = Path(
        os.environ.get(
            "HF_HUB_CACHE",
            os.path.join(
                os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")),
                "hub",
            ),
        )
    )
    # the lm studio models dir is defined in settings.json, or is ~/.lmstudio/models
    try:
        with open(
            Path(os.path.expanduser("~/.lmstudio/settings.json"))
        ) as settings_file:
            settings = json.load(settings_file)
            lm_studio_dir = Path(settings["downloadsFolder"])
    except (json.JSONDecodeError, FileNotFoundError):
        # if there's an error while reading the models dir, set it to the default
        lm_studio_dir = Path(os.path.expanduser("~/.lmstudio/models"))

    discovered = discover_models(hub_dir)
    if not discovered:
        print("No models found in Hugging Face cache")
        return

    desired = select_desired(discovered, autolink_all, lm_studio_dir)
    apply_desired_state(lm_studio_dir, discovered, desired)


def discover_models(hub_dir):
    """Discover and classify models in the Hugging Face Hub cache.

    Returns a list of (model_type, model_name, snapshot_path) tuples for
    classifiable models. Unclassifiable models are reported and skipped
    (SPEC §2.4); discovery never crashes on unusual cache layout (SPEC §2.3).
    """
    found_models = []
    seen_models = set()
    for model_dir in glob.glob(os.path.join(hub_dir, "models--*")):
        refs_dir = Path(os.path.join(model_dir, "refs"))
        snapshots_dir = Path(os.path.join(model_dir, "snapshots"))
        if not refs_dir.exists():
            continue

        # and get the model name
        parts = model_dir.split("--")
        model_name = "/".join(parts[1:])

        # Dedupe discovered models by model name: first snapshot wins.
        if model_name in seen_models:
            continue

        # Search for refs
        for ref_file in glob.glob(os.path.join(refs_dir, "*")):
            # Skip directory entries in refs/ (e.g. refs/refs/pr/1)
            # described in docs/huggingface-cache.md (SPEC §2.3).
            if not Path(ref_file).is_file():
                continue
            try:
                with open(ref_file, "r", encoding="utf-8") as f:
                    ref = f.read()
            except OSError:
                # Report and skip unreadable refs rather than crashing.
                print(f"Skipping {model_name}: could not read ref {ref_file}")
                continue
            # now have a snapshot reference, determine the snapshot path
            snapshot_path = os.path.join(snapshots_dir, ref)

            # Determine model type accurately using the helper function
            model_type = get_model_type(snapshot_path, model_name)

            # Models that cannot be classified are reported and skipped (SPEC §2.4)
            if model_type == "Unknown":
                print(f"Skipping {model_name}: could not classify model type")
                continue

            # Store model_type, model_name, and snapshot_path
            found_models.append((model_type, model_name, snapshot_path))
            seen_models.add(model_name)
            break
    return found_models


def select_desired(models, autolink_all, lm_dir):
    """Return the set of model names the user wants linked (desired state).

    With --all, every classifiable model is selected without a terminal
    (SPEC §2.2). Otherwise the interactive menu defines desired state
    (SPEC §2.6): selected models must end up linked, unselected models must
    end up unlinked.
    """
    if autolink_all:
        return {model_name for _, model_name, _ in models}

    model_choices = []
    for model_type, model_name, snapshot_path in sorted(
        models, key=lambda elem: elem[1].lower()
    ):
        target_path = lm_dir / model_name
        is_imported = target_path.is_symlink()
        status = " (already imported)" if is_imported else ""
        display_name = f"({model_type}) {model_name}{status}"
        model_choices.append((display_name, model_name, is_imported, snapshot_path))

    selected = select_models(model_choices)
    return {model_name for _, model_name, _, _ in selected}


def apply_desired_state(lm_dir, discovered, desired):
    """Make the LM Studio dir match the desired state (SPEC §2.5, §2.6).

    Selected models are linked as a directory of symlinks into the HF cache;
    unselected models have any existing link removed. Real files or
    directories that the user created are never modified or deleted; only
    symlinks we created are created or removed.
    """
    for _, model_name, snapshot_path in discovered:
        target_path = lm_dir / model_name
        if model_name in desired:
            # Selected: ensure a directory of symlinks into the snapshot.
            if target_path.exists() and not target_path.is_symlink():
                print(f"Skipping {model_name}: real file/directory at link target")
                continue
            if target_path.is_symlink():
                target_path.unlink()
            target_path.mkdir(parents=True, exist_ok=True)
            for item in Path(snapshot_path).iterdir():
                link_path = target_path / item.name
                if link_path.is_symlink():
                    # Replace an existing symlink we created.
                    link_path.unlink()
                elif link_path.exists():
                    # Never overwrite a real user file.
                    print(
                        f"Skipping {model_name}/{item.name}: real file at link target"
                    )
                    continue
                os.symlink(item, link_path)
            print(f"Linked {model_name}")
        else:
            # Not selected: remove any link we created for this model.
            if target_path.is_symlink():
                target_path.unlink()
                print(f"Unlinked {model_name}")
            elif target_path.is_dir():
                # Our link directory: drop the symlink entries we created, then
                # remove the directory if it is now empty. User files are kept.
                for child in list(target_path.iterdir()):
                    if child.is_symlink():
                        child.unlink()
                try:
                    target_path.rmdir()
                    print(f"Unlinked {model_name}")
                except OSError:
                    print(f"Unlinked {model_name}: user files kept, directory remains")


def main():
    """Entry point for uvx execution"""
    parser = argparse.ArgumentParser(
        prog="hf lmstudio",
        description="Link models in the hf cache to the LM Studio/Bionic downloads folder.",
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Link all models without interactive selection",
    )
    args = parser.parse_args()
    manage_models(autolink_all=args.all)


if __name__ == "__main__":
    main()
