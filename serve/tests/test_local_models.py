"""The model catalog and the downloader: what the models tab is built from.

Nothing here reaches Hugging Face or the network: the file listings come through a mocked
`_get_json`, and a download runs a fake `hf_hub_download` that only drives the progress reporter.
The real transfer, its retries and its resume are huggingface_hub's own code, tested upstream."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve.local import api, catalog, downloader, logs, state

INKLING = "jesdga/Inkling-2-Mini-Basalt"
PACK = "Inkling-2-Mini-NVFP4-DQ8.basalt"
GIB = 2 ** 30


def info(files):
    """The shape of a GET /api/models/<repo> answer, from a list of (name, bytes)."""
    siblings = [{"rfilename": n, "size": b, "lfs": {"sha256": f"{i:064x}"}}
                for i, (n, b) in enumerate(f for f in files
                                           if f[0].endswith(".basalt") and "/" not in f[0])]
    return {"gated": False, "siblings": siblings + [
        {"rfilename": n, "size": b, "lfs": {}} for n, b in files if not n.endswith(".basalt") or "/" in n]}


class Catalog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        patcher = mock.patch.object(state, "path", return_value=self.root / state.STATE_NAME)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_offered_repos_are_complete_enough_to_show(self):
        rows = catalog.repos()
        self.assertEqual(len(rows), len(catalog.REPOS))
        for row in rows:
            for key in ("repo", "short", "model", "arch", "kind", "note", "entry", "url", "files"):
                self.assertIn(key, row)
            self.assertTrue(row["url"].endswith(row["repo"]))
            self.assertEqual(row["entry"]["max-context"], 131072)

    def test_only_packs_of_their_own_count_and_the_smallest_comes_first(self):
        packs = catalog._packs_of(info([("b/bigger.basalt", 5 * GIB),        # a subfolder is not a pack
                                       ("small.basalt", GIB), ("readme.md", 12),
                                       ("middle.basalt", 3 * GIB)]))
        self.assertEqual([p["name"] for p in packs], ["small.basalt", "middle.basalt"])
        self.assertEqual(packs[0]["bytes"], GIB)
        self.assertEqual(packs[0]["sha256"], "0" * 64)

    def test_a_listing_is_fetched_once_and_kept(self):
        asked = []

        def fake(url):
            asked.append(url)
            return info([(PACK, 18 * GIB)])

        with mock.patch.object(catalog, "_get_json", side_effect=fake):
            rows = catalog.refresh(repo=INKLING)
            again = catalog.refresh(repo=INKLING)
            forced = catalog.refresh(repo=INKLING, force=True)

        def files_of(answer):
            return next(r for r in answer if r["repo"] == INKLING)["files"]

        self.assertEqual([f["name"] for f in files_of(rows)], [PACK])
        self.assertEqual(files_of(again), files_of(rows))
        self.assertEqual(files_of(forced), files_of(rows))
        self.assertEqual(len(asked), 2)                                    # the middle call was the cache
        self.assertTrue(files_of(rows)[0]["sha256"])

    def test_a_repo_that_cannot_be_reached_keeps_its_last_listing(self):
        with mock.patch.object(catalog, "_get_json", return_value=info([(PACK, 18 * GIB)])):
            catalog.refresh(repo=INKLING)
        with mock.patch.object(catalog, "_get_json", side_effect=OSError("no route to host")):
            rows = catalog.refresh(repo=INKLING, force=True)
        row = next(r for r in rows if r["repo"] == INKLING)
        self.assertEqual([f["name"] for f in row["files"]], [PACK])
        self.assertIn("no route to host", row["error"])

    def test_a_failed_listing_waits_a_minute_not_six_hours(self):
        now = 1000000
        self.assertTrue(catalog._stale({"error": "boom", "error_at": now - 70}, now))
        self.assertFalse(catalog._stale({"error": "boom", "error_at": now - 10}, now))
        self.assertFalse(catalog._stale({"checked_at": now - 3600, "files": [1]}, now))
        self.assertTrue(catalog._stale({"checked_at": now - 3600 * 7, "files": [1]}, now))
        self.assertTrue(catalog._stale({"checked_at": now, "files": []}, now))     # never listed

    def test_a_file_is_found_only_in_a_known_repo(self):
        with mock.patch.object(catalog, "_get_json", return_value=info([(PACK, 18 * GIB)])):
            catalog.refresh(repo=INKLING)
        meta, pack = catalog.find(INKLING, PACK)
        self.assertEqual(meta["model"], "Inkling 2 Mini")
        self.assertEqual(pack["bytes"], 18 * GIB)
        self.assertEqual(catalog.find(INKLING, "Else.basalt")[1], None)
        self.assertEqual(catalog.find("someone/else", PACK), (None, None))

    def test_the_alias_names_the_quantization_when_a_repo_has_several(self):
        one = [{"name": PACK}]
        many = [{"name": "Qwen3.8-FN-UD-IQ2_XS.basalt"}, {"name": "Qwen3.8-FN-Q8_0.basalt"}]
        with mock.patch.object(catalog, "find_list", return_value=one):
            self.assertEqual(catalog.alias_for(INKLING, PACK), "Inkling")
        repo = "jesdga/Qwen3.8-Flash-Next-Basalt"
        with mock.patch.object(catalog, "find_list", return_value=many):
            self.assertEqual(catalog.alias_for(repo, "Qwen3.8-FN-UD-IQ2_XS.basalt"),
                             "Flash-Next-UD-IQ2_XS")

    def test_an_entry_carries_the_repo_flags_and_a_relative_path(self):
        entry = catalog.entry_for(INKLING, PACK)
        self.assertEqual(entry["model"], f"models/{PACK}")
        self.assertEqual(entry["kv"], "fp8")
        self.assertEqual(entry["max-context"], 131072)
        self.assertNotIn("files", entry)

    def test_an_alias_the_config_already_uses_gets_a_number(self):
        self.assertEqual(catalog.unique_alias("Inkling", {"Flash-Next"}), "Inkling")
        self.assertEqual(catalog.unique_alias("Inkling", {"Inkling"}), "Inkling-2")
        self.assertEqual(catalog.unique_alias("Inkling", {"Inkling", "Inkling-2"}), "Inkling-3")


class WhatIsOnDisk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.models = self.root / "models"
        self.models.mkdir()
        patcher = mock.patch.object(catalog, "ROOT", new=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(state, "path", return_value=self.root / state.STATE_NAME)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_models(self, *names):
        for name in names:
            (self.models / name).write_bytes(b"x")

    def config(self, entries):
        p = self.root / "basalt_config.json"
        p.write_text(json.dumps({"models": entries}), encoding="utf-8")
        patcher = mock.patch.object(catalog, "config_path", return_value=p)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_file_the_config_does_not_name_is_reported_as_such(self):
        self.write_models(PACK, "Other.basalt")
        self.config([{"model": "models/Other.basalt", "alias": "Other"}])
        rows = catalog.installed()
        by_name = {r["name"]: r for r in rows}
        self.assertEqual([r["name"] for r in rows], ["Inkling-2-Mini-NVFP4-DQ8.basalt", "Other.basalt"])
        self.assertTrue(by_name["Other.basalt"]["in_config"])
        self.assertEqual(by_name["Other.basalt"]["entry"]["alias"], "Other")
        self.assertFalse(by_name[PACK]["in_config"])
        self.assertEqual(by_name[PACK]["suggested"]["model"], f"models/{PACK}")

    def test_a_suggested_alias_does_not_reuse_one_the_config_has(self):
        self.write_models(PACK)
        state.merge(catalog={INKLING: {"checked_at": 1, "files": [{"name": PACK, "bytes": 18 * GIB}]}})
        self.config([{"model": "models/some-other-file.basalt", "alias": "Inkling"}])
        row = catalog.installed()[0]
        self.assertEqual(row["repo"], INKLING)
        self.assertEqual(row["suggested"]["alias"], "Inkling-2")
        self.assertEqual(row["suggested"]["kv"], "fp8")

    def test_a_pack_that_is_not_in_the_catalog_still_gets_an_entry(self):
        self.write_models("Odd-Quant.basalt")
        self.config([])
        with mock.patch.object(catalog, "repos", return_value=[]):
            suggested = catalog.installed()[0]["suggested"]
        self.assertEqual(suggested, {"alias": "Odd-Quant", "model": "models/Odd-Quant.basalt"})

    def test_a_config_that_cannot_be_read_is_not_a_crash(self):
        self.write_models(PACK)
        patcher = mock.patch.object(catalog, "config_path",
                                   return_value=self.root / "absent" / "basalt_config.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(catalog.config_entries(), {})
        self.assertFalse(catalog.installed()[0]["in_config"])

    def test_a_missing_models_folder_reads_as_empty(self):
        patcher = mock.patch.object(catalog, "models_dir", return_value=self.root / "nowhere")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(catalog.installed(), [])

    def test_a_pack_needs_room_on_the_disk_beside_itself(self):
        pack = {"name": PACK, "bytes": 50 * GIB}
        with mock.patch.object(catalog, "disk", return_value={"free": 60 * GIB, "path": "m", "needs": 4 * GIB}):
            self.assertEqual(catalog.fits(pack), (True, ""))
        with mock.patch.object(catalog, "disk", return_value={"free": 40 * GIB, "path": "m", "needs": 4 * GIB}):
            ok, why = catalog.fits(pack)
        self.assertFalse(ok)
        self.assertIn("освободите место", why)

    def test_the_disk_answer_names_the_folder_and_its_room(self):
        d = catalog.disk()
        self.assertEqual(Path(d["path"]), self.models)
        self.assertGreater(d["free"], 0)
        self.assertGreater(d["needs"], 0)


class LogTail(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.p = Path(self.dir.name) / "job.log"

    def test_whole_lines_only_and_the_offset_for_the_next_read(self):
        logs.append(self.p, "первая\n")
        out = logs.tail(self.p, 0)
        self.assertEqual(out["text"].strip(), "первая")
        self.assertEqual(out["next"], len("первая\n".encode()))
        self.p.write_bytes(self.p.read_bytes() + "полови".encode())
        again = logs.tail(self.p, out["next"])
        self.assertEqual(again["text"], "")                    # a half line is not handed over
        self.assertEqual(again["next"], out["next"])
        self.p.write_bytes(self.p.read_bytes() + "строка\n".encode())
        done = logs.tail(self.p, out["next"])
        self.assertEqual(done["text"].strip(), "половистрока")

    def test_a_missing_log_and_a_stale_offset_are_both_empty(self):
        self.assertEqual(logs.tail(self.p, 0), {"text": "", "next": 0, "size": 0})
        self.p.write_text("short\n", encoding="utf-8")
        self.assertEqual(logs.tail(self.p, 4096)["text"], "")


class Downloader(unittest.TestCase):
    """A download runs in the calling thread: the fake transfer finishes at once, so the assertions
    are about what the page would see afterwards."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.models = self.root / "models"
        self.models.mkdir()
        self.log = self.root / "logs" / "download.log"
        for name, target in (("ROOT", self.root), ("LOG", self.log), ("STATE_FILE", Path("s.json"))):
            patcher = mock.patch.object(downloader, name, new=target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(state, "path", return_value=self.root / state.STATE_NAME)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(catalog, "models_dir", return_value=self.models)
        patcher.start()
        self.addCleanup(patcher.stop)
        # run the work here instead of a thread: the guards below still ask whether a worker is alive
        patcher = mock.patch.object(downloader, "_start_thread",
                                   side_effect=lambda repo, filename: downloader._download(repo, filename))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: setattr(downloader, "_run", {}))

    def allow(self):
        """Let the checks pass: the pack is known, there is room, and none of it is on disk yet."""
        pack = {"name": PACK, "bytes": 18 * GIB}
        for target, name, value in ((catalog, "find", ({"model": "Inkling 2 Mini"}, pack)),
                                    (catalog, "installed", []),
                                    (catalog, "fits", (True, ""))):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def transfer(self, fail=None):
        """A stand-in for hf_hub_download: drives the reporter the way the library does."""
        def download(repo_id, filename, local_dir, library_name, tqdm_class):
            target = Path(local_dir) / filename
            with tqdm_class(total=18 * GIB, initial=0, disable=True, desc=filename) as bar:
                for _ in range(3):
                    bar.update(6 * GIB)
            if fail:
                raise fail
            target.write_bytes(b"pack")
            return str(target)

        patcher = mock.patch.object(downloader, "_hf_download", return_value=download)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_pack_comes_to_the_models_folder_and_is_reported(self):
        self.allow()
        self.transfer()
        out = downloader.start(INKLING, PACK)
        self.assertTrue(out["started"], out)
        self.assertEqual(out["path"], str(self.models / PACK))
        self.assertTrue((self.models / PACK).is_file())
        st = downloader.status()
        self.assertFalse(st["running"])
        self.assertTrue(st["ok"])
        self.assertEqual(st["bytes_done"], 18 * GIB)
        self.assertEqual(state.load()["downloads"][PACK]["repo"], INKLING)
        self.assertIn("готово", self.log.read_text(encoding="utf-8"))

    def test_a_transfer_that_fails_is_the_run_s_own_reason(self):
        self.allow()
        self.transfer(TimeoutError("connection reset"))
        downloader.start(INKLING, PACK)
        st = downloader.status()
        self.assertFalse(st["ok"])
        self.assertIn("connection reset", st["error"])
        self.assertIn("не удалось", self.log.read_text(encoding="utf-8"))

    def test_a_second_download_while_one_runs_is_refused(self):
        alive = mock.Mock()
        alive.is_alive.return_value = True
        patcher = mock.patch.object(downloader, "_worker", new=alive)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = downloader.start(INKLING, PACK)
        self.assertFalse(out["started"])
        self.assertIn("уже идёт", out["error"])

    def test_a_repo_or_a_file_outside_the_rules_is_refused(self):
        for repo, name, phrase in (
                ("just-a-name", PACK, "репозиторий"),
                (INKLING, "../Else.basalt", "файла"),
                (INKLING, "pack.bin", ".basalt"),
        ):
            out = downloader.start(repo, name)
            self.assertFalse(out["started"], (repo, name))
            self.assertIn(phrase, out["error"])

    def test_a_pack_not_in_the_listing_is_refused(self):
        patcher = mock.patch.object(catalog, "find", return_value=(None, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(catalog, "refresh", return_value=[]):
            out = downloader.start(INKLING, "Ghost.basalt")
        self.assertFalse(out["started"])
        self.assertIn("нет в списке", out["error"])

    def test_a_pack_with_no_room_left_is_refused(self):
        patcher = mock.patch.object(catalog, "find",
                                    return_value=({"model": "x"}, {"name": PACK, "bytes": 18 * GIB}))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(catalog, "fits", return_value=(False, "нужно 22 ГиБ свободно"))
        patcher.start()
        self.addCleanup(patcher.stop)
        out = downloader.start(INKLING, PACK)
        self.assertFalse(out["started"])
        self.assertIn("22 ГиБ", out["error"])

    def test_a_pack_already_on_disk_is_not_fetched_twice(self):
        patcher = mock.patch.object(catalog, "find",
                                    return_value=({"model": "x"}, {"name": PACK, "bytes": 18 * GIB}))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(catalog, "fits", return_value=(True, ""))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(catalog, "installed",
                                   return_value=[{"name": PACK, "bytes": 18 * GIB}])
        patcher.start()
        self.addCleanup(patcher.stop)
        out = downloader.start(INKLING, PACK)
        self.assertFalse(out["started"])
        self.assertTrue(out["already"])

    def test_a_missing_library_says_what_to_install(self):
        self.allow()
        patcher = mock.patch.object(downloader, "_hf_download", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = downloader.start(INKLING, PACK)
        self.assertFalse(out["started"])
        self.assertIn("huggingface_hub", out["error"])

    def test_a_download_left_running_by_a_restart_is_called_interrupted(self):
        (self.root / "s.json").write_text(json.dumps(
            {"running": True, "file": PACK, "repo": INKLING, "bytes_done": 3 * GIB}), encoding="utf-8")
        st = downloader.status()
        self.assertFalse(st["running"])
        self.assertTrue(st["interrupted"])
        self.assertIn("прерван", st["error"])

    def test_the_log_is_tailed_from_an_offset(self):
        self.allow()
        self.transfer()
        downloader.start(INKLING, PACK)
        first = downloader.tail(0)
        self.assertIn(PACK, first["text"])
        self.assertEqual(downloader.tail(first["next"])["text"], "")
        self.assertFalse(first["running"])

    def test_the_reporter_holds_the_speed_and_the_bytes(self):
        bar = downloader._Bar(total=10 * GIB, initial=2 * GIB)
        bar.update(3 * GIB)
        bar.close()
        run = dict(downloader._run)
        self.assertEqual(bar.done, 5 * GIB)


class ModelsRoutes(unittest.TestCase):
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

    def test_the_tab_gets_the_catalog_the_disk_and_the_run_at_once(self):
        with mock.patch.object(api.catalog, "refresh", return_value=[{"repo": INKLING}]) as refresh, \
                mock.patch.object(api.catalog, "installed", return_value=[]), \
                mock.patch.object(api.catalog, "disk", return_value={"free": 1}), \
                mock.patch.object(api.downloader, "status", return_value={"running": False}):
            code, body = self.get("/api/local/models")
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"repos", "installed", "disk", "download"})
        refresh.assert_called_once_with(force=False)

    def test_the_tab_can_ask_for_a_fresh_listing(self):
        for path, force in (("/api/local/models", False), ("/api/local/models?force=1", True)):
            with mock.patch.object(api.catalog, "refresh", return_value=[]) as refresh, \
                    mock.patch.object(api.catalog, "installed", return_value=[]), \
                    mock.patch.object(api.catalog, "disk", return_value={}), \
                    mock.patch.object(api.downloader, "status", return_value={}):
                self.get(path)
            refresh.assert_called_once_with(force=force)

    def test_the_download_log_is_tailed_from_the_requested_offset(self):
        for path, since in (("/api/local/models/log", 0), ("/api/local/models/log?since=17", 17)):
            with mock.patch.object(api.downloader, "tail", return_value={}) as tail:
                code, _ = self.get(path)
            self.assertEqual(code, 200)
            tail.assert_called_once_with(since)

    def test_post_starts_the_download(self):
        body = json.dumps({"repo": INKLING, "file": PACK}).encode()
        with mock.patch.object(api.downloader, "start", return_value={"started": True}) as start:
            code, answer = self.post("/api/local/models", body)
        self.assertEqual((code, answer), (200, {"started": True}))
        start.assert_called_once_with(INKLING, PACK)

    def test_a_download_that_refused_comes_back_as_409(self):
        refused = {"started": False, "error": "загрузка уже идёт"}
        with mock.patch.object(api.downloader, "start", return_value=refused):
            code, answer = self.post("/api/local/models", b'{"repo": "a/b", "file": "c.basalt"}')
        self.assertEqual((code, answer), (409, refused))

    def test_a_download_body_without_repo_and_file_is_a_400(self):
        for body in (b"{}", b'{"repo": "a/b"}', b'{"repo": 1, "file": "c.basalt"}'):
            with mock.patch.object(api.downloader, "start") as start:
                code, answer = self.post("/api/local/models", body)
            self.assertEqual(code, 400, body)
            self.assertIn("repo", answer["error"]["message"])
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
