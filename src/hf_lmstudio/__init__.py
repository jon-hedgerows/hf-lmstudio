"""hf-lmstudio: Link models in the hf cache to the LM Studio/Bionic downloads folder."""

try:
    from importlib.metadata import version as _dist_version

    __version__ = _dist_version("hf-lmstudio")
except Exception:  # noqa: BLE001  # pragma: no cover - fallback for unbuilt source trees
    __version__ = "0.0.0"
