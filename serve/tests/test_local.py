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
        handler.path = "/api/local/state"
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


class LocalRoutes(unittest.TestCase):
    """GET and POST dispatch inside /api/local/: each name reaches its updater call."""

    def get(self, path):
        handler = mock.Mock()
        handler.path = path
        api.handle_get(handler, None, None)
        return handler._json.call_args.args

    def post(self, path, body):
        handler = mock.Mock()
        handler.path = path
        handler._body.return_value = body
        api.handle_post(handler, None, None)
        return handler._json.call_args.args

    def test_update_status_is_read_from_the_updater(self):
        with mock.patch.object(api.updater, "status", return_value={"running": True, "pid": 7}) as status:
            code, body = self.get("/api/local/update")
        self.assertEqual((code, body), (200, {"running": True, "pid": 7}))
        status.assert_called_once_with()

    def test_a_check_can_be_forced_past_the_cache(self):
        for path, force in (("/api/local/update/check", False), ("/api/local/update/check?force=1", True)):
            with mock.patch.object(api.updater, "check", return_value={}) as check:
                code, _ = self.get(path)
            self.assertEqual(code, 200)
            check.assert_called_once_with(force=force)

    def test_the_log_is_tailed_from_the_requested_offset(self):
        for path, since in (("/api/local/update/log", 0), ("/api/local/update/log?since=42", 42),
                            ("/api/local/update/log?since=notanumber", 0)):
            with mock.patch.object(api.updater, "tail", return_value={}) as tail:
                code, _ = self.get(path)
            self.assertEqual(code, 200)
            tail.assert_called_once_with(since)

    def test_an_unknown_get_is_a_404(self):
        code, body = self.get("/api/local/nothing")
        self.assertEqual(code, 404)
        self.assertIn("no local route", body["error"]["message"])

    def test_post_starts_the_run_with_the_target(self):
        for body, expected in ((b'{"target": "v0.6.0"}', "v0.6.0"), (b"{}", None)):
            with mock.patch.object(api.updater, "start", return_value={"started": True}) as start:
                code, answer = self.post("/api/local/update", body)
            self.assertEqual((code, answer), (200, {"started": True}))
            start.assert_called_once_with(expected)

    def test_a_run_that_refused_comes_back_as_409(self):
        refused = {"started": False, "error": "the working tree has uncommitted changes"}
        with mock.patch.object(api.updater, "start", return_value=refused):
            code, answer = self.post("/api/local/update", b"{}")
        self.assertEqual((code, answer), (409, refused))

    def test_a_body_that_is_not_an_object_is_rejected(self):
        for body in (b"not json", b'"a string"', b"[1, 2]"):
            with mock.patch.object(api.updater, "start") as start:
                code, answer = self.post("/api/local/update", body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", answer)
            start.assert_not_called()

    def test_a_target_must_be_a_string(self):
        with mock.patch.object(api.updater, "start") as start:
            code, answer = self.post("/api/local/update", b'{"target": 6}')
        self.assertEqual(code, 400)
        self.assertIn("target", answer["error"]["message"])
        start.assert_not_called()

    def test_an_unknown_post_is_a_404(self):
        code, _ = self.post("/api/local/nothing", b"{}")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
