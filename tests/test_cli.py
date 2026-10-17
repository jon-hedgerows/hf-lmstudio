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
from unittest import mock

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

    def test_preserve_real_files_in_directory(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        tgt = self.lm / "org/model-a"
        tgt.mkdir(parents=True)
        (tgt / "user_file.txt").write_text("keep me")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertEqual((tgt / "user_file.txt").read_text(), "keep me")
        self.assertTrue((tgt / "config.json").is_symlink(), "missing symlink added")

    def test_skip_real_file_at_target(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        tgt = self.lm / "org/model-a"
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text("real file")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertEqual(tgt.read_text(), "real file", "real file not touched")
        self.assertFalse((tgt / "config.json").exists(), "cannot create dir over file")

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


class TestIsFullyLinked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lm = Path(self.tmp) / "lm"
        self.lm.mkdir()
        self.snap = Path(tempfile.mkdtemp())
        (self.snap / "config.json").write_text('{"model_type": "llama"}')
        (self.snap / "weights.bin").write_bytes(b"\x00" * 10)

    def test_not_linked(self):
        self.assertFalse(cli.is_fully_linked(self.lm, "org/model-a", self.snap))

    def test_fully_linked(self):
        tgt = self.lm / "org" / "model-a"
        tgt.mkdir(parents=True)
        for item in self.snap.iterdir():
            (tgt / item.name).symlink_to(item)
        self.assertTrue(cli.is_fully_linked(self.lm, "org/model-a", self.snap))

    def test_partially_linked(self):
        tgt = self.lm / "org" / "model-a"
        tgt.mkdir(parents=True)
        (tgt / "config.json").symlink_to(self.snap / "config.json")
        # weights.bin missing
        self.assertFalse(cli.is_fully_linked(self.lm, "org/model-a", self.snap))

    def test_wrong_symlink_target(self):
        tgt = self.lm / "org" / "model-a"
        tgt.mkdir(parents=True)
        (tgt / "config.json").symlink_to("/tmp/wrong_target")
        (tgt / "weights.bin").symlink_to(self.snap / "weights.bin")
        self.assertFalse(cli.is_fully_linked(self.lm, "org/model-a", self.snap))

    def test_real_file_target(self):
        tgt = self.lm / "org" / "model-a"
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text("not a directory")
        self.assertFalse(cli.is_fully_linked(self.lm, "org/model-a", self.snap))

    def test_symlink_target_instead_of_dir(self):
        tgt = self.lm / "org" / "model-a"
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.symlink_to(self.snap)
        self.assertFalse(cli.is_fully_linked(self.lm, "org/model-a", self.snap))


class TestNoChangeOnConfirm(unittest.TestCase):
    """Already-imported models are pre-selected; confirming with no changes
    leaves linked models untouched (clarification to SPEC §2.6)."""

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

    def test_pre_select_already_imported(self):
        snap = self._snapshot("aaa", {
            "config.json": '{"model_type": "llama"}',
            "weights.bin": None,
        })
        discovered = [("llama", "org/model-a", str(snap))]
        # Fully link the model first.
        cli.apply_desired_state(lm_dir=self.lm, discovered=discovered, desired={"org/model-a"})
        tgt = self.lm / "org/model-a"
        # UI initialization: fully linked → pre-selected (is_fully_linked True).
        self.assertTrue(cli.is_fully_linked(self.lm, "org/model-a", snap))

    def test_confirm_with_no_changes_makes_no_changes(self):
        snap = self._snapshot("aaa", {
            "config.json": '{"model_type": "llama"}',
            "weights.bin": None,
        })
        discovered = [("llama", "org/model-a", str(snap))]
        # Fully link the model.
        cli.apply_desired_state(lm_dir=self.lm, discovered=discovered, desired={"org/model-a"})
        tgt = self.lm / "org/model-a"
        before_config = os.readlink(tgt / "config.json")
        before_weights = os.readlink(tgt / "weights.bin")
        # User confirms with no changes: model stays selected, apply_desired_state
        # is called with it in desired. Correct symlinks are skipped — nothing changes.
        cli.apply_desired_state(lm_dir=self.lm, discovered=discovered, desired={"org/model-a"})
        self.assertEqual(os.readlink(tgt / "config.json"), before_config)
        self.assertEqual(os.readlink(tgt / "weights.bin"), before_weights)
        self.assertTrue(cli.is_fully_linked(self.lm, "org/model-a", snap))


class TestApplyDesiredStateExtended(unittest.TestCase):
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

    def test_complete_partial_links(self):
        snap = self._snapshot("aaa", {
            "config.json": '{"model_type": "llama"}',
            "weights.bin": None,
        })
        discovered = [("llama", "org/model-a", str(snap))]
        # Pre-create only config.json symlink (partial link)
        tgt = self.lm / "org/model-a"
        tgt.mkdir(parents=True)
        (tgt / "config.json").symlink_to(snap / "config.json")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertTrue((tgt / "config.json").is_symlink())
        self.assertTrue((tgt / "weights.bin").is_symlink())

    def test_replace_wrong_symlink(self):
        snap = self._snapshot("aaa", {"config.json": '{"model_type": "llama"}'})
        discovered = [("llama", "org/model-a", str(snap))]
        tgt = self.lm / "org/model-a"
        tgt.mkdir(parents=True)
        (tgt / "config.json").symlink_to("/tmp/wrong_target")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertTrue((tgt / "config.json").is_symlink())
        self.assertNotEqual(os.readlink(tgt / "config.json"), "/tmp/wrong_target")

    def test_preserve_real_files_when_completing(self):
        snap = self._snapshot("aaa", {
            "config.json": '{"model_type": "llama"}',
            "weights.bin": None,
        })
        discovered = [("llama", "org/model-a", str(snap))]
        tgt = self.lm / "org/model-a"
        tgt.mkdir(parents=True)
        (tgt / "config.json").symlink_to(snap / "config.json")
        (tgt / "user_file.txt").write_text("keep me")
        cli.apply_desired_state(self.lm, discovered, {"org/model-a"})
        self.assertEqual((tgt / "user_file.txt").read_text(), "keep me")
        self.assertTrue((tgt / "weights.bin").is_symlink())


class TestSelectModels(unittest.TestCase):
    """select_models must pre-select already-imported models (SPEC §2.6)."""

    def _make_choices(self):
        # model_choices entries: (display_name, model_name, is_imported, snapshot_path)
        snap = Path(tempfile.mkdtemp())
        return [
            ("(llama) org/imported", "org/imported", True, str(snap)),
            ("(llama) org/not-imported", "org/not-imported", False, str(snap)),
            ("(llama) org/also-imported", "org/also-imported", True, str(snap)),
        ]

    def test_pre_selects_imported_models(self):
        choices = self._make_choices()
        with mock.patch.object(
            os, "get_terminal_size", return_value=types.SimpleNamespace(lines=50)
        ), mock.patch.object(cli, "get_key", return_value="\r"):
            selected = cli.select_models(choices)
        selected_names = {c[1] for c in selected}
        self.assertEqual(selected_names, {"org/imported", "org/also-imported"})
        self.assertNotIn("org/not-imported", selected_names)


class TestSelectDesired(unittest.TestCase):
    def test_autolink_all_selects_every_model(self):
        discovered = [("llama", "org/a", "/x"), ("MLX", "mlx-community/b", "/y")]
        desired = cli.select_desired(discovered, autolink_all=True, lm_dir=Path("/tmp"))
        self.assertEqual(desired, {"org/a", "mlx-community/b"})


if __name__ == "__main__":
    unittest.main()
