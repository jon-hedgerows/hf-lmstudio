"""Unit tests for hf_lmstudio.cli."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hf_lmstudio.cli import (
    DiscoveredModel,
    ModelChoice,
    ModelFormat,
    _classify_via_config,
    _link_model,
    _resolve_hub_dir,
    _resolve_lm_studio_dir,
    _unlink_model,
    apply_desired_state,
    cleanup_dangling_links,
    discover_models,
    get_model_type,
    is_fully_linked,
    manage_models,
    select_desired,
    select_models,
)


class TestClassifyViaConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_config_returns_none(self):
        self.assertIsNone(_classify_via_config(self.path))

    def test_present_model_type_returns_other(self):
        (self.path / "config.json").write_text(
            json.dumps({"model_type": "llama"}), encoding="utf-8"
        )
        self.assertEqual(_classify_via_config(self.path), ModelFormat.OTHER)

    def test_empty_model_type_returns_none(self):
        (self.path / "config.json").write_text(
            json.dumps({"model_type": ""}), encoding="utf-8"
        )
        self.assertIsNone(_classify_via_config(self.path))

    def test_non_string_model_type_returns_none(self):
        (self.path / "config.json").write_text(
            json.dumps({"model_type": 42}), encoding="utf-8"
        )
        self.assertIsNone(_classify_via_config(self.path))

    def test_invalid_json_returns_none(self):
        (self.path / "config.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(_classify_via_config(self.path))

    def test_non_object_json_returns_none(self):
        (self.path / "config.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertIsNone(_classify_via_config(self.path))


class TestGetModelType(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_directory_returns_unknown(self):
        self.assertEqual(get_model_type(self.path / "missing", "org/repo"), ModelFormat.UNKNOWN)

    def test_gguf_recursive(self):
        sub = self.path / "sub"
        sub.mkdir()
        (sub / "model.gguf").write_text("x", encoding="utf-8")
        self.assertEqual(get_model_type(self.path, "org/repo"), ModelFormat.GGUF)

    def test_mlx_org(self):
        self.assertEqual(get_model_type(self.path, "mlx-community/foo"), ModelFormat.MLX)

    def test_mlx_org_case_insensitive(self):
        self.assertEqual(get_model_type(self.path, "MLX-Community/foo"), ModelFormat.MLX)

    def test_config_fallback(self):
        (self.path / "config.json").write_text(
            json.dumps({"model_type": "bert"}), encoding="utf-8"
        )
        self.assertEqual(get_model_type(self.path, "org/repo"), ModelFormat.OTHER)

    def test_unknown(self):
        self.assertEqual(get_model_type(self.path, "org/repo"), ModelFormat.UNKNOWN)


class TestDiscoverModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self.tmp.name) / "hub"
        self.hub.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_model(self, name, snapshot, ref_content):
        model_dir = self.hub / f"models--{name.replace('/', '--')}"
        (model_dir / "refs").mkdir(parents=True)
        (model_dir / "snapshots" / snapshot).mkdir(parents=True)
        (model_dir / "refs" / "main").write_text(ref_content, encoding="utf-8")
        return model_dir

    def test_empty_hub(self):
        self.assertEqual(discover_models(self.hub), [])

    def test_missing_hub(self):
        self.assertEqual(discover_models(self.hub / "nope"), [])

    def test_discovers_model(self):
        self._make_model("org/repo", "abc123", "abc123")
        models = discover_models(self.hub)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].model_name, "org/repo")
        self.assertEqual(models[0].snapshot_path.name, "abc123")

    def test_strips_ref_whitespace(self):
        self._make_model("org/repo", "abc123", "abc123\n")
        models = discover_models(self.hub)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].snapshot_path.name, "abc123")

    def test_skips_missing_snapshot(self):
        self._make_model("org/repo", "abc123", "zzz")
        models = discover_models(self.hub)
        self.assertEqual(models, [])

    def test_dedupes_by_model_name(self):
        self._make_model("org/repo", "abc123", "abc123")
        # second dir with same derived name cannot happen via glob naming,
        # so create a duplicate refs entry instead by adding another ref file.
        model_dir = self.hub / "models--org--repo"
        (model_dir / "refs" / "dev").write_text("abc123", encoding="utf-8")
        models = discover_models(self.hub)
        self.assertEqual(len(models), 1)


class TestIsFullyLinked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.hub = self.root / "hub"
        self.lm.mkdir()
        self.hub.mkdir()
        self.snapshot = self.hub / "snapshots" / "abc"
        self.snapshot.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_returns_false(self):
        self.assertFalse(is_fully_linked(self.lm, "org/repo", self.snapshot))

    def test_real_dir_returns_false(self):
        (self.lm / "org" / "repo").mkdir(parents=True)
        self.assertFalse(is_fully_linked(self.lm, "org/repo", self.snapshot))

    def test_symlink_to_snapshot_returns_true(self):
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(self.snapshot)
        self.assertTrue(is_fully_linked(self.lm, "org/repo", self.snapshot))

    def test_symlink_to_sibling_returns_true(self):
        sibling = self.hub / "snapshots" / "def"
        sibling.mkdir(parents=True)
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(sibling)
        self.assertTrue(is_fully_linked(self.lm, "org/repo", self.snapshot))

    def test_symlink_elsewhere_returns_false(self):
        other = self.root / "other"
        other.mkdir()
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(other)
        self.assertFalse(is_fully_linked(self.lm, "org/repo", self.snapshot))

    def test_broken_symlink_returns_false(self):
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(self.hub / "nope")
        self.assertFalse(is_fully_linked(self.lm, "org/repo", self.snapshot))


class TestLinkModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.lm.mkdir()
        self.snapshot = self.root / "snap" / "abc"
        self.snapshot.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_symlink(self):
        _link_model(self.lm, "org/repo", self.snapshot)
        link = self.lm / "org" / "repo"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.snapshot.resolve())

    def test_replaces_existing_symlink(self):
        (self.lm / "org").mkdir()
        old = self.root / "old"
        old.mkdir()
        (self.lm / "org" / "repo").symlink_to(old)
        _link_model(self.lm, "org/repo", self.snapshot)
        link = self.lm / "org" / "repo"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.snapshot.resolve())

    def test_does_not_overwrite_real_dir(self):
        (self.lm / "org" / "repo").mkdir(parents=True)
        _link_model(self.lm, "org/repo", self.snapshot)
        self.assertTrue((self.lm / "org" / "repo").is_dir())
        self.assertFalse((self.lm / "org" / "repo").is_symlink())

    def test_replaces_broken_symlink(self):
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(self.root / "nope")
        _link_model(self.lm, "org/repo", self.snapshot)
        link = self.lm / "org" / "repo"
        self.assertTrue(link.is_symlink())


class TestUnlinkModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.lm.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unlinks_symlink(self):
        (self.lm / "org").mkdir()
        target = self.root / "snap"
        target.mkdir()
        (self.lm / "org" / "repo").symlink_to(target)
        _unlink_model(self.lm, "org/repo")
        self.assertFalse((self.lm / "org" / "repo").exists())
        self.assertFalse((self.lm / "org" / "repo").is_symlink())

    def test_leaves_real_dir(self):
        (self.lm / "org" / "repo").mkdir(parents=True)
        _unlink_model(self.lm, "org/repo")
        self.assertTrue((self.lm / "org" / "repo").is_dir())

    def test_missing_is_noop(self):
        _unlink_model(self.lm, "org/repo")  # should not raise


class TestApplyDesiredState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.lm.mkdir()
        self.snap = self.root / "snap" / "abc"
        self.snap.mkdir(parents=True)
        self.model = DiscoveredModel(ModelFormat.GGUF, "org/repo", self.snap)

    def tearDown(self):
        self.tmp.cleanup()

    def test_links_desired_and_unlinks_undesired(self):
        # pre-link an undesired model
        (self.lm / "org").mkdir()
        other = self.root / "other"
        other.mkdir()
        (self.lm / "org" / "other").symlink_to(other)

        other_model = DiscoveredModel(ModelFormat.GGUF, "org/other", self.snap)
        apply_desired_state(self.lm, [self.model, other_model], {"org/repo"})
        self.assertTrue((self.lm / "org" / "repo").is_symlink())
        self.assertFalse((self.lm / "org" / "other").exists())


class TestCleanupDanglingLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.hub = self.root / "hub"
        self.lm.mkdir()
        self.hub.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_dangling_link_into_hub(self):
        target = self.hub / "snapshots" / "gone"
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(target)
        cleanup_dangling_links(self.lm, self.hub)
        self.assertFalse((self.lm / "org" / "repo").exists())
        self.assertFalse((self.lm / "org" / "repo").is_symlink())

    def test_leaves_dangling_link_elsewhere(self):
        (self.lm / "org").mkdir()
        (self.lm / "org" / "repo").symlink_to(self.root / "nope")
        cleanup_dangling_links(self.lm, self.hub)
        self.assertTrue((self.lm / "org" / "repo").is_symlink())

    def test_prunes_empty_dirs(self):
        (self.lm / "org").mkdir()
        target = self.hub / "snapshots" / "gone"
        (self.lm / "org" / "repo").symlink_to(target)
        cleanup_dangling_links(self.lm, self.hub)
        self.assertFalse((self.lm / "org").exists())

    def test_does_not_remove_lm_root(self):
        cleanup_dangling_links(self.lm, self.hub)
        self.assertTrue(self.lm.is_dir())

    def test_missing_lm_dir_is_noop(self):
        cleanup_dangling_links(self.lm / "nope", self.hub)  # should not raise


class TestSelectDesired(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lm = self.root / "lm"
        self.lm.mkdir()
        self.snap = self.root / "snap" / "abc"
        self.snap.mkdir(parents=True)
        self.models = [DiscoveredModel(ModelFormat.GGUF, "org/repo", self.snap)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_autolink_all(self):
        self.assertEqual(
            select_desired(self.models, True, self.lm, False), {"org/repo"}
        )

    def test_just_clean_without_import_returns_empty(self):
        self.assertEqual(
            select_desired(self.models, False, self.lm, True), set()
        )

    def test_empty_models(self):
        self.assertEqual(select_desired([], False, self.lm, False), set())


class TestSelectModels(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(select_models([]), [])

    def test_preselected_imported_confirmed(self):
        snap = Path("/tmp/snap")
        choices = [ModelChoice("name", "org/repo", True, snap)]
        with patch("hf_lmstudio.cli.get_key", return_value="\r"):
            result = select_models(choices)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].model_name, "org/repo")


class TestResolveHelpers(unittest.TestCase):
    def test_hub_dir_default(self):
        with patch.dict(os.environ, {}, clear=True):
            # remove relevant vars
            os.environ.pop("HF_HUB_CACHE", None)
            os.environ.pop("HF_HOME", None)
            path = _resolve_hub_dir()
            self.assertTrue(str(path).endswith("huggingface/hub"))

    def test_hub_dir_env(self):
        with patch.dict(os.environ, {"HF_HUB_CACHE": "/tmp/myhub"}, clear=False):
            self.assertEqual(_resolve_hub_dir(), Path("/tmp/myhub"))

    def test_empty_env_falls_back(self):
        with patch.dict(os.environ, {"HF_HUB_CACHE": ""}, clear=False):
            os.environ.pop("HF_HOME", None)
            path = _resolve_hub_dir()
            self.assertTrue(str(path).endswith("huggingface/hub"))

    def test_lm_studio_settings(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            settings.write_text(
                json.dumps({"downloadsFolder": "/tmp/lm"}), encoding="utf-8"
            )
            with patch("hf_lmstudio.cli.LM_STUDIO_SETTINGS", str(settings)):
                self.assertEqual(_resolve_lm_studio_dir(), Path("/tmp/lm"))


class TestManageModels(unittest.TestCase):
    def test_missing_hub_prints_no_models(self):
        with tempfile.TemporaryDirectory() as td:
            hub = Path(td) / "hub"
            lm = Path(td) / "lm"
            lm.mkdir()
            with patch("hf_lmstudio.cli._resolve_hub_dir", return_value=hub), \
                    patch("hf_lmstudio.cli._resolve_lm_studio_dir", return_value=lm):
                manage_models(autolink_all=True)
            # should not raise; lm dir remains
            self.assertTrue(lm.is_dir())


if __name__ == "__main__":
    unittest.main()
