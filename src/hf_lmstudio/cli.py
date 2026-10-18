import argparse
import json
import logging
import os
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

# credit: much of this was written by Ivan Fioravanti

logger = logging.getLogger("hf_lmstudio")

# Terminal key sequences (R7: named constants instead of magic literals).
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_ENTER = "\r"
KEY_CTRL_C = "\x03"

# Seconds to wait for a Hub metadata request (N4: applied to model_info()).
HUB_TIMEOUT_SECONDS = 30


class ModelFormat(str, Enum):
    """Model file format classification (SPEC §2.4).

    Using str, Enum keeps values comparable to plain strings while giving
    an explicit, documented set of formats.
    """

    GGUF = "GGUF"
    MLX = "MLX"
    OTHER = "Other"
    UNKNOWN = "Unknown"


# Shared HfApi client, created lazily and reused across models (F6).
_hf_api: object | None = None


def _get_hf_api():
    """Return a shared HfApi client, or None if huggingface_hub is absent.

    huggingface_hub is provided by the `hf` runtime under uvx, not a hard
    install-time dependency (SPEC §4), so the import is lazy.
    """
    global _hf_api
    if _hf_api is None:
        try:
            from huggingface_hub import HfApi

            _hf_api = HfApi()
        except ImportError:
            _hf_api = None
    return _hf_api


def _fetch_model_info(api, repo_id):
    """Call ``api.model_info`` with a timeout (N4).

    ``timeout`` was added to ``HfApi.model_info`` in newer huggingface_hub
    releases; fall back to a call without it on versions that reject it.
    """
    try:
        return api.model_info(repo_id, timeout=HUB_TIMEOUT_SECONDS)
    except TypeError:
        return api.model_info(repo_id)


def _hub_offline() -> bool:
    """True when ``HF_HUB_OFFLINE`` is set to a truthy value (F6)."""
    value = os.environ.get("HF_HUB_OFFLINE", "").strip().lower()
    return value not in ("", "0", "false", "no")


def _warn(message: str) -> None:
    """Print a warning/status message to stderr (R5)."""
    print(message, file=sys.stderr)


class DiscoveredModel(NamedTuple):
    """A model discovered in the Hugging Face cache (R1)."""

    model_type: ModelFormat
    model_name: str
    snapshot_path: Path


class ModelChoice(NamedTuple):
    """A model presented to the user in the interactive selector (R1)."""

    display_name: str
    model_name: str
    is_imported: bool
    snapshot_path: Path


def select_models(model_choices: list[ModelChoice]) -> list[ModelChoice]:
    # Pre-select models that are already imported (SPEC §2.6 clarification):
    # a fully and correctly symlinked model shows as selected in the UI.
    selected = [bool(choice.is_imported) for choice in model_choices]
    idx = 0
    # R6: guard terminal size; fall back to a sensible default when no TTY
    # or when the computed window would be non-positive.
    try:
        window_size = os.get_terminal_size().lines - 5
    except OSError:
        window_size = 20
    window_size = max(window_size, 1)

    while True:
        print("\033[H\033[J", end="")
        print(
            "❯ hf lmstudio - Hugging Face LM Studio Model Link \nAvailable models (↑/↓ to navigate, SPACE to select, ENTER to confirm, Ctrl+C to quit):"
        )

        window_start = max(
            0, min(idx - window_size + 3, len(model_choices) - window_size)
        )
        window_end = min(window_start + window_size, len(model_choices))

        for i in range(window_start, window_end):
            display_name = model_choices[i].display_name
            print(
                f"{'>' if i == idx else ' '} {'◉' if selected[i] else '○'} {display_name}"
            )

        key = get_key()
        if key == KEY_UP:
            idx = max(0, idx - 1)
        elif key == KEY_DOWN:
            idx = min(len(model_choices) - 1, idx + 1)
        elif key == " ":
            selected[idx] = not selected[idx]
        elif key == KEY_ENTER:
            break
        elif key == KEY_CTRL_C:
            print("\nImport is cancelled. Do nothing.")
            sys.exit(0)

    return [
        choice
        for choice, is_selected in zip(model_choices, selected, strict=False)
        if is_selected
    ]


def get_key():
    """Get a single keypress from the user."""
    import termios
    import tty

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


def _classify_via_hub(repo_id: str) -> ModelFormat | None:
    """Hub-metadata tier (SPEC §2.4, F6, N4).

    Returns a format when the Hub tags/``library_name``/``gguf`` metadata
    identifies one, otherwise None. A shared client is reused across models,
    ``model_info()`` is given a timeout, and ``HF_HUB_OFFLINE`` short-circuits
    the call entirely.
    """
    api = _get_hf_api()
    if api is None or _hub_offline():
        return None
    try:
        info = _fetch_model_info(api, repo_id)
        library = (getattr(info, "library_name", None) or "").lower()
        tags = {str(t).lower() for t in (getattr(info, "tags", None) or [])}
        if library == "gguf" or "gguf" in tags or getattr(info, "gguf", None):
            return ModelFormat.GGUF
        if library == "mlx" or "mlx" in tags:
            return ModelFormat.MLX
    except OSError as exc:
        # HfHubHTTPError (404/401/...) is an OSError, as are transport errors
        # from the HTTP layer: report and fall through.
        logger.warning("Hub lookup failed for %s: %s", repo_id, exc)
    except Exception:
        # Unexpected, non-network error: never let a single model abort
        # discovery, but surface it rather than swallowing it silently.
        logger.warning(
            "Unexpected error classifying %s via the Hub", repo_id, exc_info=True
        )
    return None


def _classify_via_config(path: Path) -> ModelFormat | None:
    """config.json fallback: any present ``model_type`` means "Other".

    Architecture is deliberately not part of the format contract (SPEC §2.4).
    """
    config_path = path / "config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = config.get("model_type")
    if isinstance(value, str) and value:
        return ModelFormat.OTHER
    return None


def get_model_type(snapshot_path: str | Path, repo_id: str) -> ModelFormat:
    """Classify a model's file format (SPEC §2.4).

    Tiers, in order: local GGUF check, mlx-community organisation check,
    Hub metadata (``library_name``/tags/``gguf``) via huggingface_hub,
    config.json model_type fallback. Returns ModelFormat.UNKNOWN when nothing
    applies; such models are included in the discovery list for user selection
    (clarification to SPEC §2.4).
    """
    path = Path(snapshot_path)
    if not path.is_dir():
        return ModelFormat.UNKNOWN

    # 1. Local GGUF check (recursive: snapshots preserve subdirectories,
    #    and GGUF repos commonly place files in per-quantisation folders).
    if any(p.is_file() and p.suffix.lower() == ".gguf" for p in path.rglob("*")):
        return ModelFormat.GGUF

    # 2. Organisation check (case-insensitive).
    if repo_id.lower().startswith("mlx-community/"):
        return ModelFormat.MLX

    # 3 & 4. Hub metadata, then config.json fallback.
    result = _classify_via_hub(repo_id)
    if result is None:
        result = _classify_via_config(path)

    # 5. Could not classify.
    return result if result is not None else ModelFormat.UNKNOWN


def manage_models(autolink_all: bool = False) -> None:
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
        settings_path = Path(os.path.expanduser("~/.lmstudio/settings.json"))
        with open(settings_path) as settings_file:
            settings = json.load(settings_file)
        downloads_folder = settings.get("downloadsFolder")
        if downloads_folder:
            lm_studio_dir = Path(downloads_folder)
        else:
            lm_studio_dir = Path(os.path.expanduser("~/.lmstudio/models"))
    except (json.JSONDecodeError, FileNotFoundError, KeyError, OSError):
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


def discover_models(hub_dir: str | Path) -> list[DiscoveredModel]:
    """Discover and classify models in the Hugging Face Hub cache.

    Returns a list of DiscoveredModel records. Unclassifiable models are
    labelled ModelFormat.UNKNOWN and included so the user can decide whether
    to link them (clarification to SPEC §2.4); discovery never crashes on
    unusual cache layout (SPEC §2.3).
    """
    found_models: list[DiscoveredModel] = []
    seen_models: set[str] = set()
    hub_path = Path(hub_dir)
    for model_dir in hub_path.glob("models--*"):
        refs_dir = model_dir / "refs"
        snapshots_dir = model_dir / "snapshots"
        if not refs_dir.exists():
            continue

        # R4: derive repo id from the final path component only.
        model_name = model_dir.name.removeprefix("models--").replace("--", "/")

        # Dedupe discovered models by model name: first snapshot wins.
        if model_name in seen_models:
            continue

        # Search for refs
        for ref_file in refs_dir.glob("*"):
            # Skip directory entries in refs/ (e.g. refs/refs/pr/1)
            # described in docs/huggingface-cache.md (SPEC §2.3).
            if not ref_file.is_file():
                continue
            try:
                with open(ref_file, encoding="utf-8") as f:
                    ref = f.read()
            except OSError:
                # Report and skip unreadable refs rather than crashing.
                _warn(f"Skipping {model_name}: could not read ref {ref_file}")
                continue
            # now have a snapshot reference, determine the snapshot path
            snapshot_path = snapshots_dir / ref
            if not snapshot_path.is_dir():
                # Snapshot no longer in the cache (model removed). Skip it from
                # discovery so any existing link is left for
                # cleanup_dangling_links to remove.
                _warn(f"Skipping {model_name}: snapshot no longer in cache")
                continue

            # Determine model type accurately using the helper function.
            model_type = get_model_type(snapshot_path, model_name)

            # Store model_type, model_name, and snapshot_path
            found_models.append(DiscoveredModel(model_type, model_name, snapshot_path))
            seen_models.add(model_name)
            break
    return found_models


def is_fully_linked(lm_dir: str | Path, model_name: str, snapshot_path: str | Path) -> bool:
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
            # realpath resolves the symlink target relative to its location,
            # which is required for relative symlink targets in the HF cache.
            if os.path.realpath(link_path) != os.path.realpath(item):
                return False
        except OSError:
            return False
    return True


def select_desired(
    models: list[DiscoveredModel],
    autolink_all: bool,
    lm_dir: str | Path,
) -> set[str]:
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
        return {model.model_name for model in models}

    model_choices: list[ModelChoice] = []
    for model in sorted(models, key=lambda m: m.model_name.lower()):
        is_imported = is_fully_linked(lm_dir, model.model_name, model.snapshot_path)
        status = " (already imported)" if is_imported else ""
        display_name = f"({model.model_type.value}) {model.model_name}{status}"
        model_choices.append(
            ModelChoice(display_name, model.model_name, is_imported, model.snapshot_path)
        )

    selected = select_models(model_choices)
    return {choice.model_name for choice in selected}


def _link_snapshot_item(target_path: Path, item: Path, model_name: str) -> None:
    """Create or correct a single symlink for a snapshot entry (SPEC §2.5)."""
    link_path = target_path / item.name
    if link_path.is_symlink():
        # Keep a correct symlink; replace a wrong one. realpath resolves
        # relative symlink targets against the link's location (HF cache
        # snapshots use relative paths).
        try:
            if os.path.realpath(link_path) == os.path.realpath(item):
                return
        except OSError:
            pass
        link_path.unlink()
    elif link_path.exists():
        # Real user file/directory: never overwrite.
        _warn(f"Skipping {model_name}/{item.name}: real file at link target")
        return
    os.symlink(item, link_path)


def _link_model(lm_dir: Path, model_name: str, snapshot_path: Path) -> None:
    """Link every snapshot file into ``lm_dir/model_name`` (SPEC §2.5)."""
    target_path = lm_dir / model_name
    if (
        target_path.exists()
        and not target_path.is_dir()
        and not target_path.is_symlink()
    ):
        # Real file at the link target: never touch it.
        _warn(f"Skipping {model_name}: real file at link target")
        return
    if target_path.is_symlink():
        # Replace an existing symlink with a directory of symlinks.
        target_path.unlink()
    target_path.mkdir(parents=True, exist_ok=True)
    for item in Path(snapshot_path).iterdir():
        _link_snapshot_item(target_path, item, model_name)
    print(f"Linked {model_name}")


def _unlink_model(lm_dir: Path, model_name: str) -> None:
    """Remove links we created for ``model_name``, keeping user files (§2.6)."""
    target_path = lm_dir / model_name
    if target_path.is_symlink():
        target_path.unlink()
        print(f"Unlinked {model_name}")
    elif target_path.is_dir():
        # Our link directory: drop the symlink entries we created, then remove
        # the directory if it is now empty. User files are kept.
        for child in list(target_path.iterdir()):
            if child.is_symlink():
                child.unlink()
        try:
            target_path.rmdir()
            print(f"Unlinked {model_name}")
        except OSError:
            print(f"Unlinked {model_name}: user files kept, directory remains")


def apply_desired_state(
    lm_dir: str | Path,
    discovered: list[DiscoveredModel],
    desired: set[str],
) -> None:
    """Make the LM Studio dir match the desired state (SPEC §2.5, §2.6).

    Selected models are linked as a directory of symlinks into the HF cache;
    unselected models have any existing link removed. Real files or
    directories that the user created are never modified or deleted; only
    symlinks we created are created or removed.
    """
    lm_dir = Path(lm_dir)
    for model in discovered:
        if model.model_name in desired:
            _link_model(lm_dir, model.model_name, model.snapshot_path)
        else:
            _unlink_model(lm_dir, model.model_name)


def cleanup_dangling_links(lm_dir: str | Path, hub_dir: str | Path) -> None:
    """Remove dangling symlinks that point into the HF cache, then prune
    now-empty directories (SPEC §2.6).

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
    for root, _dirs, _files in os.walk(lm_dir, topdown=False):
        root_path = Path(root)
        if root_path == lm_dir:
            continue
        try:
            root_path.rmdir()  # succeeds only when the directory is empty
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
    args = parser.parse_args()
    if not args.all and not sys.stdin.isatty():
        parser.error(
            "no terminal available for interactive selection; "
            "use --all to link every model without a prompt"
        )
    manage_models(autolink_all=args.all)


if __name__ == "__main__":
    main()
