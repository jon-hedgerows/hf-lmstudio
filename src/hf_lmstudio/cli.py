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
    """Classify a model as GGUF, MLX, or another type (SPEC §2.4).

    Tiers, in order: local GGUF check, mlx-community organisation check,
    Hub metadata (library/tags) via huggingface_hub, config.json model_type
    fallback. Returns "Unknown" when nothing applies; callers skip those
    models (SPEC §2.4).
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.is_dir():
        return "Unknown"

    # 1. Local GGUF check.
    if any(item.suffix == ".gguf" for item in snapshot_path.iterdir()):
        return "GGUF"

    # 2. Organisation check.
    if repo_id.startswith("mlx-community/"):
        return "MLX"

    # 3. Hub metadata (huggingface_hub is provided by the `hf` runtime, not a
    #    hard dependency -- imported lazily, SPEC §4).
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id)
        library = getattr(info, "library", None) or ""
        tags = getattr(info, "tags", None) or []
        tag_set = {str(t).lower() for t in tags}
        if library.lower() == "gguf" or "gguf" in tag_set:
            return "GGUF"
        if library.lower() == "mlx" or "mlx" in tag_set:
            return "MLX"
    except Exception:
        # ImportError, network error, private model, etc.: fall through.
        pass

    # 4. config.json fallback.
    config_path = snapshot_path / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            model_type = config.get("model_type", "").lower()
            if model_type:
                return model_type
        except (json.JSONDecodeError, OSError):
            pass

    # 5. Could not classify.
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

    desired = select_desired(discovered, autolink_all, lm_studio_dir)
    if discovered:
        print(f"\nLinking {len(desired)} of {len(discovered)} models...\n")
    apply_desired_state(lm_studio_dir, discovered, desired)
    # Always clean up dangling links, even when nothing was discovered: a
    # model removed from the Hub cache leaves dangling links behind
    # (SPEC §2.6, §6.3).
    cleanup_dangling_links(lm_studio_dir, hub_dir)


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
            if not Path(snapshot_path).is_dir():
                # Snapshot no longer in the cache (model removed). Skip it from
                # discovery so any existing link is left for
                # cleanup_dangling_links to remove (SPEC §6.3).
                print(f"Skipping {model_name}: snapshot no longer in cache")
                continue

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


def is_fully_linked(lm_dir, model_name, snapshot_path):
    """Return True if every file in ``snapshot_path`` is present as a correct
    symlink in ``lm_dir/model_name`` (SPEC §2.6 UI initialization).

    A model shows in the UI as **selected** only when it is already fully and
    correctly symlinked; it shows as **unselected** when not found in LM
    Studio or when the symlinks are incomplete.
    """
    target_path = lm_dir / model_name
    if not target_path.is_dir():
        return False
    snapshot_path = Path(snapshot_path)
    for item in snapshot_path.iterdir():
        link_path = target_path / item.name
        if not link_path.is_symlink():
            return False
        try:
            if Path(os.readlink(link_path)).resolve() != item.resolve():
                return False
        except OSError:
            return False
    return True


def select_desired(models, autolink_all, lm_dir):
    """Return the set of model names the user wants linked (desired state).

    With --all, every classifiable model is selected without a terminal
    (SPEC §2.2). Otherwise the interactive menu defines desired state
    (SPEC §2.6): selected models must end up linked, unselected models must
    end up unlinked.

    UI initialization: a model shows as selected when it is already fully and
    correctly symlinked in LM Studio; it shows as unselected when not found
    or when the symlinks are incomplete.
    """
    if autolink_all:
        return {model_name for _, model_name, _ in models}

    model_choices = []
    for model_type, model_name, snapshot_path in sorted(
        models, key=lambda elem: elem[1].lower()
    ):
        is_imported = is_fully_linked(lm_dir, model_name, snapshot_path)
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


def cleanup_dangling_links(lm_dir, hub_dir):
    """Remove dangling symlinks that point into the HF cache, then prune
    now-empty directories (SPEC §2.6, §6.3).

    This also removes links for models that have been removed from the Hub
    cache: once the snapshot disappears, those symlinks dangle and are
    cleaned up here. Only symlinks resolving inside hub_dir are candidates;
    other dangling links are left alone. lm_dir itself is never removed.
    """
    hub_dir = Path(hub_dir).resolve()
    lm_dir = Path(lm_dir)

    # Bottom-up: handle entries before their containing directories.
    for root, dirs, files in os.walk(lm_dir, topdown=False):
        root_path = Path(root)
        for name in list(files) + list(dirs):
            entry = root_path / name
            if not entry.is_symlink():
                continue
            # Dangling symlink: the target no longer exists on disk.
            if os.path.exists(entry):
                continue
            try:
                target = os.readlink(entry)
                resolved = (root_path / target).resolve()
            except OSError:
                continue
            # Only remove links that point into the Hugging Face cache.
            if resolved == hub_dir or hub_dir in resolved.parents:
                entry.unlink()

    # Prune empty directories left behind, never removing lm_dir itself.
    for root, dirs, files in os.walk(lm_dir, topdown=False):
        root_path = Path(root)
        if root_path == lm_dir:
            continue
        try:
            root_path.rmdir()  # succeeds only when the directory is empty
        except OSError:
            pass


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
