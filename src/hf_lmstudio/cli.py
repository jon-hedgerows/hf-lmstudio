"""hf-lmstudio: Link models in the hf cache to the LM Studio/Bionic downloads folder."""
import argparse
import json
import logging
import os
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

try:
    from hf_lmstudio import __version__
except Exception:  # noqa: BLE001  # pragma: no cover
    __version__ = "0.0.0"

# credit: much of this was written by Ivan Fioravanti

logger = logging.getLogger("hf_lmstudio")

# Terminal key sequences.
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_ENTER = "\r"
KEY_CTRL_C = "\x03"

# Default locations.
DEFAULT_HF_HOME = "~/.cache/huggingface"
DEFAULT_LM_STUDIO_MODELS = "~/.lmstudio/models"
LM_STUDIO_SETTINGS = "~/.lmstudio/settings.json"


class ModelFormat(str, Enum):
    """Model file format classification (SPEC section 2.4)."""

    GGUF = "GGUF"
    MLX = "MLX"
    OTHER = "Other"
    UNKNOWN = "Unknown"


def _warn(message: str) -> None:
    """Print a warning/status message to stderr."""
    print(message, file=sys.stderr)


class DiscoveredModel(NamedTuple):
    """A model discovered in the Hugging Face cache."""

    model_type: ModelFormat
    model_name: str
    snapshot_path: Path


class ModelChoice(NamedTuple):
    """A model presented to the user in the interactive selector."""

    display_name: str
    model_name: str
    is_imported: bool
    snapshot_path: Path


def select_models(model_choices: list[ModelChoice]) -> list[ModelChoice]:
    """Render the interactive selector and return the chosen models."""
    if not model_choices:
        return []

    selected = [bool(choice.is_imported) for choice in model_choices]
    idx = 0
    try:
        window_size = os.get_terminal_size().lines - 5
    except OSError:
        window_size = 20
    window_size = max(window_size, 1)

    while True:
        if sys.stdout.isatty():
            print("\033[H\033[J", end="")
        print(
            "❯ hf lmstudio - Hugging Face to LM Studio Model Link \n"
            "Available models (↑/↓ to navigate, SPACE to select, ENTER to confirm, "
            "Ctrl+C to quit):"
        )

        window_start = max(
            0, min(idx - window_size + 3, len(model_choices) - window_size)
        )
        window_end = min(window_start + window_size, len(model_choices))

        for i in range(window_start, window_end):
            display_name = model_choices[i].display_name
            print(
                f"{'>' if i == idx else ' '} "
                f"{'◉' if selected[i] else '○'} {display_name}"
            )

        key = get_key()
        if key == KEY_UP:
            idx = max(0, idx - 1)
        elif key == KEY_DOWN:
            idx = min(len(model_choices) - 1, idx + 1)
        elif key == " ":
            selected[idx] = not selected[idx]
        elif key in (KEY_ENTER, "\n"):
            break
        elif key == KEY_CTRL_C:
            print("\nImport is cancelled. Do nothing.")
            sys.exit(130)

    return [
        choice
        for choice, is_selected in zip(model_choices, selected, strict=True)
        if is_selected
    ]


def get_key() -> str:
    """Read a single keypress from stdin."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if not ch:
            return ""
        if ch == "\x1b":
            ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def _classify_via_config(path: Path) -> ModelFormat | None:
    """config.json fallback: any present ``model_type`` means "Other"."""
    config_path = path / "config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    value = config.get("model_type")
    if isinstance(value, str) and value:
        return ModelFormat.OTHER
    return None


def get_model_type(snapshot_path: str | Path, repo_id: str) -> ModelFormat:
    """Classify a model's file format (SPEC section 2.4)."""
    path = Path(snapshot_path)
    if not path.is_dir():
        return ModelFormat.UNKNOWN

    if any(
        p.is_file() and p.suffix.lower() == ".gguf" for p in path.rglob("*")
    ):
        return ModelFormat.GGUF

    if repo_id.lower().startswith("mlx-community/"):
        return ModelFormat.MLX

    result = _classify_via_config(path)
    return result if result is not None else ModelFormat.UNKNOWN


def _resolve_hub_dir() -> Path:
    """Resolve the Hugging Face Hub cache directory."""
    hf_hub_cache = os.environ.get("HF_HUB_CACHE", "").strip()
    if hf_hub_cache:
        return Path(hf_hub_cache).expanduser()
    hf_home = os.environ.get("HF_HOME", "").strip()
    if hf_home:
        return (Path(hf_home).expanduser() / "hub")
    return (Path(DEFAULT_HF_HOME).expanduser() / "hub")


def _resolve_lm_studio_dir() -> Path:
    """Resolve LM Studio's downloads folder."""
    settings_path = Path(LM_STUDIO_SETTINGS).expanduser()
    try:
        with settings_path.open(encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
        if isinstance(settings, dict):
            downloads_folder = settings.get("downloadsFolder")
            if isinstance(downloads_folder, str) and downloads_folder.strip():
                return Path(downloads_folder).expanduser()
    except (OSError, ValueError, KeyError):
        pass
    return Path(DEFAULT_LM_STUDIO_MODELS).expanduser()


def manage_models(
    autolink_all: bool = False,
    just_clean: bool = False,
    dry_run: bool = False,
) -> None:
    """Link models from the Hugging Face cache."""
    hub_dir = _resolve_hub_dir().resolve(strict=False)
    lm_studio_dir = _resolve_lm_studio_dir().resolve(strict=False)

    discovered = discover_models(hub_dir)
    if not discovered:
        print("No models found in Hugging Face cache")

    desired = select_desired(discovered, autolink_all, lm_studio_dir, just_clean)
    if dry_run:
        print("Dry run: no changes will be made.")
        print(f"Would link ({len(desired)}): {', '.join(sorted(desired)) or '(none)'}")
        would_unlink = {
            model.model_name for model in discovered
            if model.model_name not in desired
        }
        print(
            f"Would unlink ({len(would_unlink)}): "
            f"{', '.join(sorted(would_unlink)) or '(none)'}"
        )
        print("Would remove dangling symlinks pointing into the Hugging Face cache.")
        return

    if discovered:
        print(f"\nLinking {len(desired)} of {len(discovered)} models...\n")
    apply_desired_state(lm_studio_dir, discovered, desired)
    cleanup_dangling_links(lm_studio_dir, hub_dir)


def discover_models(hub_dir: str | Path) -> list[DiscoveredModel]:
    """Discover and classify models in the Hugging Face Hub cache."""
    found_models: list[DiscoveredModel] = []
    seen_models: set[str] = set()
    hub_path = Path(hub_dir)
    if not hub_path.is_dir():
        return found_models

    for model_dir in hub_path.glob("models--*"):
        refs_dir = model_dir / "refs"
        snapshots_dir = model_dir / "snapshots"
        if not refs_dir.is_dir():
            continue

        model_name = model_dir.name.removeprefix("models--").replace("--", "/")
        if model_name in seen_models:
            continue

        ref_candidates: list[Path] = []
        main_ref = refs_dir / "main"
        if main_ref.is_file():
            ref_candidates.append(main_ref)
        ref_candidates.extend(
            p for p in refs_dir.glob("*")
            if p.is_file() and p != main_ref
        )

        for ref_file in ref_candidates:
            try:
                with ref_file.open(encoding="utf-8") as f:
                    ref = f.read().strip()
            except OSError:
                _warn(f"Skipping {model_name}: could not read ref {ref_file}")
                continue
            if not ref:
                continue

            snapshot_path = snapshots_dir / ref
            if not snapshot_path.is_dir():
                _warn(f"Skipping {model_name}: snapshot no longer in cache")
                continue

            model_type = get_model_type(snapshot_path, model_name)
            found_models.append(
                DiscoveredModel(model_type, model_name, snapshot_path)
            )
            seen_models.add(model_name)
            break

    return found_models


def is_fully_linked(
    lm_dir: Path, model_name: str, snapshot_path: Path
) -> bool:
    """Return True if ``lm_dir/model_name`` links to this model's snapshot dir."""
    target_path = lm_dir / model_name
    if not target_path.is_symlink():
        return False

    try:
        raw_target = target_path.readlink()
        resolved_target = (
            raw_target if raw_target.is_absolute()
            else target_path.parent / raw_target
        ).resolve(strict=False)
        return (
            resolved_target.parent
            == Path(snapshot_path).resolve(strict=False).parent
        )
    except (OSError, RuntimeError, ValueError):
        return False


def select_desired(
    models: list[DiscoveredModel],
    autolink_all: bool,
    lm_dir: Path,
    just_clean: bool = False,
) -> set[str]:
    """Return the set of model names the user wants linked."""
    if autolink_all:
        return {model.model_name for model in models}

    model_choices: list[ModelChoice] = []
    for model in sorted(models, key=lambda m: m.model_name.lower()):
        is_imported = is_fully_linked(
            lm_dir, model.model_name, model.snapshot_path
        )
        status = " (already imported)" if is_imported else ""
        display_name = f"({model.model_type.value}) {model.model_name}{status}"
        model_choices.append(
            ModelChoice(
                display_name, model.model_name, is_imported, model.snapshot_path
            )
        )

    if not model_choices:
        return set()

    if not just_clean:
        selected = select_models(model_choices)
        return {choice.model_name for choice in selected}
    return {choice.model_name for choice in model_choices if choice.is_imported}


def _link_model(lm_dir: Path, model_name: str, snapshot_path: Path) -> None:
    """Link the snapshot dir into ``lm_dir/model_name``."""
    target_path = lm_dir / model_name
    if any(part in ("", ".", "..") for part in Path(model_name).parts):
        _warn(f"Skipping {model_name}: link target escapes LM Studio directory")
        return

    resolved_snapshot = Path(snapshot_path).resolve(strict=False)
    if target_path.is_symlink():
        try:
            target_path.unlink()
        except OSError as exc:
            _warn(f"Skipping {model_name}: unable to replace link ({exc})")
            return
    elif target_path.exists():
        _warn(f"Skipping {model_name}: real file or directory at link target")
        return

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.symlink_to(resolved_snapshot)
    except OSError as exc:
        _warn(f"Skipping {model_name}: unable to create link ({exc})")
        return
    print(f"Linked {model_name}")


def _unlink_model(lm_dir: Path, model_name: str) -> None:
    """Remove a link created for ``model_name``."""
    target_path = lm_dir / model_name
    if not target_path.is_symlink():
        return
    try:
        target_path.unlink()
        print(f"Unlinked {model_name}")
    except OSError as exc:
        _warn(f"Skipping {model_name}: unable to unlink ({exc})")


def apply_desired_state(
    lm_dir: Path, discovered: list[DiscoveredModel], desired: set[str]
) -> None:
    """Make the LM Studio dir match the desired state."""
    root = Path(lm_dir).resolve(strict=False)
    for model in discovered:
        try:
            if model.model_name in desired:
                _link_model(root, model.model_name, model.snapshot_path)
            else:
                _unlink_model(root, model.model_name)
        except OSError as exc:
            _warn(f"Failed to update {model.model_name}: {exc}")


def cleanup_dangling_links(lm_dir: Path, hub_dir: Path) -> None:
    """Remove dangling symlinks into the HF cache and prune empty dirs."""
    root = Path(lm_dir).resolve(strict=False)
    hub_root = Path(hub_dir).resolve(strict=False)
    if not root.is_dir():
        return

    for walk_root, _dirs, _files in os.walk(root, topdown=False):
        root_path = Path(walk_root)
        for name in list(_files) + list(_dirs):
            entry = root_path / name
            if not entry.is_symlink() or entry.exists():
                continue
            try:
                target = os.readlink(entry)
                resolved = (root_path / target).resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if resolved != hub_root and hub_root not in resolved.parents:
                continue
            try:
                entry.unlink()
            except OSError:
                continue

    for walk_root, _dirs, _files in os.walk(root, topdown=False):
        root_path = Path(walk_root)
        if root_path == root:
            continue
        try:
            root_path.rmdir()
        except OSError:
            pass


def main() -> None:
    """Entry point for uvx execution."""
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
    parser.add_argument(
        "--clean",
        "-c",
        action="store_true",
        help="Clean the LM Studio/Bionic downloads folder, (re)linking previously selected models, removing dangling symlinks, but not adding anything new",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hf-lmstudio {__version__}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned changes without modifying the filesystem",
    )

    args = parser.parse_args()
    if not (args.all or args.clean) and not sys.stdin.isatty():
        parser.error(
            "no terminal available for interactive selection; "
            "use --all to link every model without a prompt, or "
            "use --clean to relink/clean models without a prompt"
        )

    try:
        manage_models(
            autolink_all=args.all, just_clean=args.clean, dry_run=args.dry_run
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        _warn(f"error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
