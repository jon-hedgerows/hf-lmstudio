"""Tests for hf_lmstudio.cli (stdlib only, per SPEC §4).

These exercise the separable pieces of `manage_models` without needing a
terminal or the real Hugging Face cache / LM Studio directories.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import hf_lmstudio.cli as cli  # noqa: E402


def make_cache_entry(hub, repo_id, refs, snapshots):
    """Create a `models--<safe>` cache entry.

    refs: {ref_name: commit_hash}
    snapshots: {commit_hash: {filename: content}}  (content None => a .gguf blob)
    """
    safe = repo_id.replace("/", "--")
    mdir = hub / f"models--{safe}"
    (mdir / "refs").mkdir(parents=True)
    (mdir / "snapshots").mkdir(parents=True)
    for ref_name, commit in refs.items():
        (mdir / "refs" / ref_name).write_text(commit)
    for commit, files in snapshots.items():
        snap = mdir / "snapshots" / commit
        snap.mkdir()
        for fn, content in files.items():
            p = snap / fn
            if content is None:
                p.write_bytes(b"\x00" * 10)
            else:
                p.write_text(content)
    return mdir


class TestDiscoverModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hub = Path(self.tmp) / "hub"
        self.hub.mkdir()

    def test_layout_and_refs_resolution(self):
        make_cache_entry(
            self.hub,
            "org/alpha",
            {"main": "aaa"},
            {"aaa": {"config.json": '{"model_type": "llama"}'}},
        )
        found = cli.discover_models(str(self.hub))
        self.assertEqual(len(found), 1)
        mt, name, snap = found[0]
        self.assertEqual(name, "org/alpha")
        self.assertEqual(mt, "llama")
        self.assertTrue(Path(snap).is_dir())

    def test_multiple_refs_yield_single_entry(self):
        make_cache_entry(
            self.hub,
            "org/beta",
            {"main": "aaa", "v1.0": "bbb"},
            {
                "aaa": {"config.json": '{"model_type": "llama"}'},
                "bbb": {"config.json": '{"model_type": "llama"}'},
            },
        )
        found = cli.discover_models(str(self.hub))
        self.assertEqual(len(found), 1, "first valid ref wins")

    def test_subdir_ref_skipped(self):
        make_cache_entry(
            self.hub,
            "org/gamma",
            {"main": "aaa"},
            {"aaa": {"config.json": '{"model_type": "llama"}'}},
        )
        # A subdirectory inside refs/ (e.g. refs/refs/pr/1) must not crash discovery.
        (self.hub / "models--org--gamma" / "refs" / "somedir").mkdir()
        found = cli.discover_models(str(self.hub))
        self.assertEqual(len(found), 1)

    def test_unreadable_ref_skipped(self):
        make_cache_entry(
            self.hub,
            "org/delta",
            {"main": "aaa"},
            {"aaa": {"config.json": '{"model_type": "llama"}'}},
        )
        ref_dir = self.hub / "models--org--delta" / "refs"
        bad = ref_dir / "unreadable"
        bad.write_text("aaa")
        bad.chmod(0o000)
        try:
            found = cli.discover_models(str(self.hub))
            self.assertEqual(len(found), 1)
        finally:
            bad.chmod(0o644)

    def test_unclassifiable_model_skipped(self):
        make_cache_entry(
            self.hub,
            "org/epsilon",
            {"main": "aaa"},
            {"aaa": {"README.md": "hi"}},  # no .gguf, not mlx-community, no config
        )
        found = cli.discover_models(str(self.hub))
        self.assertEqual(found, [], "unclassifiable models are skipped")

    def test_missing_snapshot_skipped(self):
        make_cache_entry(
            self.hub,
            "org/zeta",
            {"main": "missing-commit"},
            {},  # snapshot dir never created
        )
        found = cli.discover_models(str(self.hub))
        self.assertEqual(found, [], "missing snapshot is skipped, not crashed")


class TestGetModelType(unittest.TestCase):
    def test_gguf_local(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "model.gguf").write_bytes(b"\x00" * 10)
        (snap / "config.json").write_text('{"model_type": "llama"}')
        self.assertEqual(cli.get_model_type(snap, "org/any"), "GGUF")

    def test_mlx_community(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "config.json").write_text('{"model_type": "llama"}')
        self.assertEqual(cli.get_model_type(snap, "mlx-community/whisper"), "MLX")

    def test_config_json_fallback(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "config.json").write_text('{"model_type": "Llama"}')
        self.assertEqual(cli.get_model_type(snap, "org/llama"), "llama")

    def test_unknown_when_nothing_identifiable(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "README.md").write_text("hi")
        self.assertEqual(cli.get_model_type(snap, "org/x"), "Unknown")

    def test_nonexistent_snapshot_is_unknown(self):
        self.assertEqual(cli.get_model_type(Path("/no/such/path"), "org/x"), "Unknown")

    def test_hub_api_overrides_misleading_config(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "config.json").write_text('{"model_type": "llama"}')
        fake = types.ModuleType("huggingface_hub")

        class FakeInfo:
            library = "gguf"
            tags = []

        class FakeHfApi:
            def model_info(self, repo_id):
                return FakeInfo()

        fake.HfApi = FakeHfApi
        sys.modules["huggingface_hub"] = fake
        try:
            self.assertEqual(cli.get_model_type(snap, "org/whatever"), "GGUF")
        finally:
            del sys.modules["huggingface_hub"]

    def test_hub_api_error_falls_through(self):
        snap = Path(tempfile.mkdtemp())
        (snap / "config.json").write_text('{"model_type": "bert"}')
        fake = types.ModuleType("huggingface_hub")

        class Boom:
            def model_info(self, repo_id):
                raise RuntimeError("network down")

        fake.HfApi = Boom
        sys.modules["huggingface_hub"] = fake
        try:
            self.assertEqual(cli.get_model_type(snap, "org/x"), "bert")
        finally:
            del sys.modules["huggingface_hub"]


class TestApplyDesiredState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lm = Path(self.tmp) / "lm"
        self.lm.mkdir()
        self.hub = Path(self.tmp) / "hub"
        self.hub.mkdir()

    def _snapshot(self, name, files):
        snap = self.hub / "snapshots" / name
        snap.mkdir(parents=True)
        for fn, content in files.items():
            p = snap / fn
            if content is None:
                p.write_bytes(b"\x00" * 10)
            else:
                p.write_text(content)
        return snap

    def test_link_selected(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        tgt = self.lm / "org/model-a"
        self.assertTrue(tgt.is_dir())
        self.assertTrue((tgt / "config.json").is_symlink())

    def test_unlink_unselected(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        # Pre-create a symlink (as if previously linked).
        tgt = self.lm / "org/model-a"
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.symlink_to(snap)
        cli.apply_desired_state(self.lm, discovered, set())
        self.assertFalse(tgt.exists(), "unselected symlink removed")

    def test_skip_real_file_directory(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        tgt = self.lm / "org/model-a"
        tgt.mkdir(parents=True)
        (tgt / "user_file.txt").write_text("keep me")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertEqual((tgt / "user_file.txt").read_text(), "keep me")
        self.assertFalse((tgt / "config.json").exists(), "real dir not overwritten")

    def test_idempotent_relink(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertTrue((self.lm / "org/model-a" / "config.json").is_symlink())


class TestCleanupDanglingLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lm = Path(self.tmp) / "lm"
        self.lm.mkdir()
        self.hub = Path(self.tmp) / "hub"
        self.hub.mkdir()

    def test_removes_dangling_cache_links_and_empty_dirs(self):
        # (a) dangling symlink into hub -> removed
        (self.lm / "dangling_cache").symlink_to(self.hub / "snapshots" / "nope" / "f.bin")
        # (b) empty subdir -> pruned
        (self.lm / "emptydir").mkdir()
        # (c) valid symlink into hub (target exists) -> kept
        real = self.hub / "keep.bin"
        real.write_text("x")
        (self.lm / "valid_link").symlink_to(real)
        # (d) real file -> kept
        (self.lm / "real_file.txt").write_text("keep me")
        # (e) dangling symlink outside hub -> kept
        (self.lm / "dangling_outside").symlink_to("/tmp/does-not-exist/x")

        cli.cleanup_dangling_links(self.lm, self.hub)

        self.assertFalse((self.lm / "dangling_cache").exists())
        self.assertFalse((self.lm / "emptydir").exists())
        self.assertTrue((self.lm / "valid_link").is_symlink())
        self.assertEqual((self.lm / "real_file.txt").read_text(), "keep me")
        self.assertTrue((self.lm / "dangling_outside").is_symlink())


class TestSelectDesired(unittest.TestCase):
    def test_autolink_all_selects_every_model(self):
        discovered = [("llama", "org/a", "/x"), ("MLX", "mlx-community/b", "/y")]
        desired = cli.select_desired(discovered, autolink_all=True, lm_dir=Path("/tmp"))
        self.assertEqual(desired, {"org/a", "mlx-community/b"})


if __name__ == "__main__":
    unittest.main()
