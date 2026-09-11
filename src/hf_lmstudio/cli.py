import json
import os
from pathlib import Path
import sys
import shutil
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

    found_models = set()
    for model_dir in glob.glob(os.path.join(hub_dir, "models--*")):
        refs_dir = Path(os.path.join(model_dir, "refs"))
        snapshots_dir = Path(os.path.join(model_dir, "snapshots"))
        if not refs_dir.exists():
            continue

        # Search for refs
        for ref_file in glob.glob(os.path.join(refs_dir, "*")):
            with open(ref_file, "r", encoding="utf-8") as f:
                ref = f.read()
                # now have a snapshot reference, determine the snapshot path
                snapshot_path = os.path.join(snapshots_dir, ref)
                # and get the model name
                parts = model_dir.split("--")

                model_name = "/".join(parts[1:])

                # Determine model type accurately using the helper function
                model_type = get_model_type(snapshot_path, model_name)

                # Store model_type, model_name, and snapshot_path
                found_models.add((model_type, model_name, snapshot_path))

    if not found_models:
        print("No models found in Hugging Face cache")
        return

    if autolink_all:
        # When autolink_all is true, select all found models
        ## TODO: update is_imported based on whether it really is or not... or just set it to selected
        selected = [
            (model_type, model, is_imported, snapshot_path)
            for model_type, model, is_imported, snapshot_path in found_models
        ]
        print("\nImporting all models...\n")
    else:
        # Create list of models with their current import status
        model_choices = []

        for model_type, model, snapshot_path in sorted(
            found_models, key=lambda elem: elem[1].lower()
        ):
            target_path = lm_studio_dir / f"{model}"
            is_imported = target_path.exists()
            status = " (already imported)" if is_imported else ""
            display_name = f"({model_type}) {model}{status}"
            model_choices.append((display_name, model, is_imported, snapshot_path))

        # Show interactive selection menu
        selected = select_models(model_choices)
        print("\nImporting models...\n")

    ## TODO: update to link selected things, and delink unselected things
    for display_name, model_name, is_imported, snapshot_path in selected:
        target_path = lm_studio_dir / f"{model_name}"

        if is_imported:
            # Remove existing directory or symlink
            if target_path.is_symlink() or target_path.exists():
                if target_path.is_dir():
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
            print(f"Removed {model_name}")

        else:
            # Create parent directories and target directory
            target_path.mkdir(parents=True, exist_ok=True)

            # Create symbolic links for all files in the snapshot directory
            for item in Path(snapshot_path).iterdir():
                link_path = target_path / item.name
                os.symlink(item, link_path)

            print(f"Imported {model_name} (symlinked files)")


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
