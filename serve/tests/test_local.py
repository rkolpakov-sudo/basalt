"""The local layer: the version block from git and its cache, the state file, and the state endpoint."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve.local import api, state, version


class VersionBlock(unittest.TestCase):
    def setUp(self):
        version.refresh()

    def tearDown(self):
        version.refresh()

    def test_the_block_names_the_local_and_the_upstream_tag(self):
        tags = {"local-*": "local-0.5.5", "v*": "v0.5.5"}
        with mock.patch.object(version, "_tag", side_effect=lambda p: tags[p]), \
                mock.patch.object(version, "_repo", return_value={"git": True, "branch": "local"}):
            block = version.info()
        self.assertEqual(block["local"], "local-0.5.5")
        self.assertEqual(block["upstream"], "v0.5.5")
        self.assertIsNone(block["engine"])            # no router: the engine is simply unknown

    def test_without_git_the_tags_are_unknown_not_guessed(self):
        with mock.patch.object(version, "_tag", return_value=None), \
                mock.patch.object(version, "_repo", return_value={"git": False}):
            block = version.info()
        self.assertIsNone(block["local"])
        self.assertIsNone(block["upstream"])
        self.assertFalse(block["repo"]["git"])

    def test_the_git_answers_are_cached_for_the_ttl(self):
        asked = []
        with mock.patch.object(version, "_tag", side_effect=lambda p: asked.append(p) or "t"), \
                mock.patch.object(version, "_repo", return_value={"git": True}):
            version.info()
            version.info()
            version.info()
        self.assertEqual(asked, ["local-*", "v*"])     # two patterns, asked once

    def test_refresh_drops_the_cache(self):
        asked = []
        with mock.patch.object(version, "_tag", side_effect=lambda p: asked.append(p) or "t"), \
                mock.patch.object(version, "_repo", return_value={"git": True}):
            version.info()
            version.refresh()
            version.info()
        self.assertEqual(len(asked), 4)

    def test_a_caller_cannot_edit_the_cache_through_the_block(self):
        with mock.patch.object(version, "_tag", return_value="t"), \
                mock.patch.object(version, "_repo", return_value={"git": True, "branch": "local"}):
            version.info()["repo"]["branch"] = "hacked"
            self.assertEqual(version.info()["repo"]["branch"], "local")

    def test_the_engine_version_comes_from_the_loaded_engine(self):
        router = mock.Mock()
        router.shown.return_value = mock.Mock(engine=mock.Mock(info={"version": "0.5.5"}))
        with mock.patch.object(version, "_tag", return_value=None), \
                mock.patch.object(version, "_repo", return_value={"git": False}):
            self.assertEqual(version.info(router)["engine"], "0.5.5")

    def test_an_engine_without_info_is_unknown(self):
        router = mock.Mock()
        router.shown.return_value = mock.Mock(engine=mock.Mock(info=None))
        self.assertIsNone(version.engine_version(router))

    def test_a_failing_router_does_not_raise(self):
        class Broken:
            def shown(self):
                raise RuntimeError("shutting down")

        self.assertIsNone(version.engine_version(Broken()))


class StateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.p = Path(self.dir.name) / state.STATE_NAME
        patcher = mock.patch.object(state, "path", return_value=self.p)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_file_reads_as_the_defaults(self):
        self.assertEqual(state.load(), state.DEFAULTS)

    def test_a_broken_file_reads_as_the_defaults(self):
        self.p.write_text("{ not json", encoding="utf-8")
        self.assertEqual(state.load(), state.DEFAULTS)

    def test_a_file_that_is_not_an_object_reads_as_the_defaults(self):
        self.p.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(state.load(), state.DEFAULTS)

    def test_merge_keeps_the_sections_it_does_not_touch(self):
        state.merge(update={"upstream": "v0.6.0"})
        state.merge(downloads={"jesdga/Inkling-2-Mini-Basalt": {"done": True}})
        data = state.load()
        self.assertEqual(data["update"], {"upstream": "v0.6.0"})
        self.assertEqual(data["downloads"], {"jesdga/Inkling-2-Mini-Basalt": {"done": True}})

    def test_merge_keeps_the_other_keys_of_the_same_section(self):
        state.merge(update={"upstream": "v0.6.0", "checked": 1})
        state.merge(update={"checked": 2})
        self.assertEqual(state.load()["update"], {"upstream": "v0.6.0", "checked": 2})

    def test_a_section_from_a_newer_version_survives(self):
        self.p.write_text(json.dumps({"update": {}, "experimental": {"a": 1}}), encoding="utf-8")
        self.assertEqual(state.load()["experimental"], {"a": 1})
        state.merge(prefs={"view": "chat"})
        self.assertEqual(state.load()["experimental"], {"a": 1})

    def test_a_write_leaves_no_temp_file_behind(self):
        state.merge(prefs={"view": "chat"})
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()], [state.STATE_NAME])

    def test_save_replaces_the_whole_file(self):
        state.merge(prefs={"view": "chat"})
        state.save({"update": {"upstream": "v0.6.0"}})
        self.assertEqual(state.load(), {"update": {"upstream": "v0.6.0"}, "downloads": {}, "prefs": {}})


class StateEndpoint(unittest.TestCase):
    def test_the_endpoint_reports_the_version_the_state_and_the_paths(self):
        handler = mock.Mock()
        with mock.patch.object(api.version, "info", return_value={"local": "local-0.5.5"}), \
                mock.patch.object(api.state, "load", return_value={"update": {}}):
            api.handle_get(handler, None, None)
        code, body = handler._json.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(body["version"], {"local": "local-0.5.5"})
        self.assertEqual(body["state"], {"update": {}})
        for key in ("root", "models", "bin", "config", "state"):
            self.assertTrue(body["paths"][key], key)

    def test_the_paths_point_at_this_install(self):
        paths = api.paths()
        self.assertTrue(paths["models"].endswith("models"))
        self.assertEqual(Path(paths["root"]), Path(paths["models"]).parent)
        self.assertTrue(paths["state"].endswith(state.STATE_NAME))


if __name__ == "__main__":
    unittest.main()
