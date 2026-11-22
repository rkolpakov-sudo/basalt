"""The updater's own logic: version order, the check, the log tail, the guards before a run.

Nothing here touches the network, git or PowerShell: `updater.version`, `updater.state`, the file
paths and `_wmi_create` are replaced, so what is under test is the decisions -- is this tag newer,
is this run already going, may this run start at all -- and the byte arithmetic of the log tail.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from serve.local import updater

GIT = {"git": True, "branch": "local", "commit": "abc1234", "dirty": False, "ahead": 0, "behind": 0}
CLEAN = {"local": "local-0.5.5", "upstream": "v0.5.5", "engine": "0.5.5", "repo": GIT}


class FakeState:
    """The two calls updater makes on the state file."""

    def __init__(self, update=None):
        self.data = {"update": dict(update or {}), "downloads": {}, "prefs": {}}

    def load(self):
        return json.loads(json.dumps(self.data))

    def merge(self, **sections):
        for key, value in sections.items():
            self.data[key] = {**self.data.get(key, {}), **value}


class FakeVersion:
    """git as a table: each command answers a canned (code, out, err)."""

    def __init__(self, answers=None, repo=None):
        self.answers = answers or {}
        self.repo = repo if repo is not None else GIT
        self.calls = []

    def info(self, router=None):
        return {**CLEAN, "repo": self.repo}

    def _tag(self, pattern):
        return "v0.5.5"

    def run(self, *args, timeout=None):
        self.calls.append(args)
        for key, answer in self.answers.items():
            if args[:len(key)] == key:
                return answer
        return (0, "", "")

    def _git(self, *args, timeout=None):
        r = self.run(*args, timeout=timeout)
        if r is None or r[0] != 0:
            return None
        return r[1].strip() or None


FETCH_OK = {
    ("fetch",): (0, "", ""),
    ("tag", "--list", "v*"): (0, "v0.4.1\nv0.5.0\nv0.5.5\n", ""),
    ("remote", "get-url"): (0, "https://github.com/jesdga95/basalt", ""),
}


class Tags(unittest.TestCase):
    def test_numeric_parts_sort_as_numbers_not_letters(self):
        self.assertGreater(updater.tag_key("v0.5.10"), updater.tag_key("v0.5.9"))
        self.assertGreater(updater.tag_key("v0.10.0"), updater.tag_key("v0.9.9"))
        self.assertEqual(updater.tag_key("v1.2.3"), (1, 2, 3))

    def test_a_tag_without_numeric_parts_still_has_a_key(self):
        self.assertEqual(updater.tag_key("garbage"), (0,))

    def test_the_newest_release_wins_over_the_highest_name(self):
        tags = ["v0.4.1", "v0.5.9", "v0.5.10", "v0.5.5"]
        self.assertEqual(updater.latest_of(tags), "v0.5.10")

    def test_local_and_malformed_tags_are_not_releases(self):
        self.assertIsNone(updater.latest_of(["local-0.5.5", "v0.5", "release-3", "", "0.6.0"]))
        self.assertIsNone(updater.latest_of([]))
        self.assertEqual(updater.latest_of(["local-0.6.0", "v0.6.0"]), "v0.6.0")


class GitQuestions(unittest.TestCase):
    def test_a_merged_tag_is_the_honest_nothing_to_do(self):
        with mock.patch.object(updater, "version", FakeVersion({("merge-base",): (0, "", "")})):
            self.assertTrue(updater._is_merged("v0.5.5"))
        with mock.patch.object(updater, "version", FakeVersion({("merge-base",): (1, "", "")})):
            self.assertFalse(updater._is_merged("v0.6.0"))
        with mock.patch.object(updater, "version", mock.Mock(run=mock.Mock(return_value=None))):
            self.assertFalse(updater._is_merged("v0.6.0"))

    def test_upstream_is_added_once_when_it_is_missing(self):
        answers = {("remote", "get-url", "upstream"): (0, "https://example.invalid", "")}
        with mock.patch.object(updater, "version", FakeVersion(answers)) as git:
            self.assertTrue(updater._ensure_remote())
            self.assertEqual(git.calls, [("remote", "get-url", "upstream")])   # no second add

        answers = {("remote", "get-url", "upstream"): (2, "", "no such remote"),
                   ("remote", "add"): (0, "", "")}
        with mock.patch.object(updater, "version", FakeVersion(answers)) as git:
            self.assertTrue(updater._ensure_remote())
            self.assertEqual(git.calls[-1], ("remote", "add", "upstream", updater.REMOTE_URL))

        answers = {("remote", "get-url", "upstream"): (2, "", ""), ("remote", "add"): (128, "", "offline")}
        with mock.patch.object(updater, "version", FakeVersion(answers)):
            self.assertFalse(updater._ensure_remote())


class Check(unittest.TestCase):
    def run_check(self, *, answers=None, state=None, repo=None, merged=False, commits=0, force=False):
        git = FakeVersion(answers or FETCH_OK, repo=repo)
        st = state or FakeState()
        with mock.patch.object(updater, "version", git), mock.patch.object(updater, "state", st), \
                mock.patch.object(updater, "_is_merged", return_value=merged), \
                mock.patch.object(updater, "_commits_between", return_value=commits):
            return updater.check(force=force), git, st

    def test_a_fresh_answer_is_reused_instead_of_fetching_again(self):
        st = FakeState({"checked_at": time.time(), "latest": "v0.5.5", "current": "v0.5.5",
                        "available": False, "commits": 0})
        out, git, _ = self.run_check(state=st)
        self.assertTrue(out["cached"])
        self.assertEqual(out["latest"], "v0.5.5")
        self.assertFalse(out["available"])
        self.assertEqual(git.calls, [])                      # not a single git call

    def test_force_asks_again(self):
        st = FakeState({"checked_at": time.time(), "latest": "v0.5.5"})
        out, git, _ = self.run_check(state=st, force=True)
        self.assertFalse(out["cached"])
        self.assertIn(("fetch", "--tags", "--prune", "upstream"), git.calls)

    def test_a_stale_answer_is_fetched_again(self):
        st = FakeState({"checked_at": time.time() - 3600, "latest": "v0.5.0"})
        out, git, saved = self.run_check(state=st)
        self.assertFalse(out["cached"])
        self.assertIn(("fetch", "--tags", "--prune", "upstream"), git.calls)
        self.assertEqual(out["latest"], "v0.5.5")
        self.assertEqual(saved.data["update"]["latest"], "v0.5.5")        # and it is remembered

    def test_a_newer_release_is_reported_as_available_with_how_much(self):
        out, _, _ = self.run_check(merged=False, commits=7)
        self.assertTrue(out["available"])
        self.assertEqual(out["commits"], 7)
        self.assertEqual(out["current"], "v0.5.5")

    def test_a_fetch_failure_names_the_reason(self):
        answers = {**FETCH_OK, ("fetch",): (128, "",
                     "fatal: unable to access: Failed to connect to github.com port 443")}
        out, _, saved = self.run_check(answers=answers)
        self.assertIn("error", out)
        self.assertIn("git fetch upstream failed", out["error"])
        self.assertIn("Failed to connect", out["error"])
        self.assertNotIn("latest", out)
        self.assertEqual(saved.data["update"], {})                        # nothing half-written

    def test_git_missing_entirely_is_not_a_fetch_failure(self):
        answers = {**FETCH_OK, ("fetch",): None}
        out, _, _ = self.run_check(answers=answers)
        self.assertIn("git could not be started", out["error"])

    def test_a_non_git_install_says_so(self):
        out, git, _ = self.run_check(repo={"git": False})
        self.assertFalse(out["git"])
        self.assertIn("not a git checkout", out["error"])
        self.assertEqual(git.calls, [])

    def test_no_tags_after_the_fetch_is_its_own_error(self):
        answers = {**FETCH_OK, ("tag", "--list", "v*"): (0, "", "")}
        out, _, _ = self.run_check(answers=answers)
        self.assertIn("no release tags", out["error"])

    def test_a_dirty_tree_is_carried_into_the_answer(self):
        dirty = {**GIT, "dirty": True}
        out, _, _ = self.run_check(repo=dirty)
        self.assertTrue(out["dirty"])
        self.assertEqual(out["branch"], "local")


class LogTail(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="basalt-updater-test-"))
        self.log = self.dir / "update.log"
        self.patcher = mock.patch.object(updater, "LOG", self.log)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        status = mock.patch.object(updater, "status", return_value={"running": False})
        status.start()
        self.addCleanup(status.stop)

    def write(self, text: str):
        self.log.write_bytes(text.encode("utf-8"))

    def test_a_finished_log_is_returned_whole_and_the_offset_is_its_end(self):
        self.write("one\ntwo\n")
        out = updater.tail(0)
        self.assertEqual(out["text"], "one\ntwo\n")
        self.assertEqual(out["next"], 8)
        self.assertEqual(out["size"], 8)

    def test_a_half_written_line_is_held_back(self):
        self.write("one\ntwo")                       # the writer has not reached the newline yet
        out = updater.tail(0)
        self.assertEqual(out["text"], "one\n")
        self.assertEqual(out["next"], 4)
        again = updater.tail(out["next"])
        self.assertEqual(again["text"], "")          # nothing to show, ask again later
        self.assertEqual(again["next"], 4)

    def test_reading_on_a_line_boundary_keeps_characters_intact(self):
        self.write("привет\nмир")                     # the last line is still being written
        first = updater.tail(0)
        self.assertEqual(first["text"], "привет\n")   # the multi-byte word is not cut in half
        self.assertEqual(first["next"], len("привет\n".encode("utf-8")))
        self.assertEqual(updater.tail(first["next"])["text"], "")
        self.write("привет\nмир\n")                   # now it is finished
        rest = updater.tail(first["next"])
        self.assertEqual(rest["text"], "мир\n")
        self.assertEqual(rest["next"], first["next"] + len("мир\n".encode("utf-8")))

    def test_an_offset_past_the_end_returns_nothing_at_the_end(self):
        self.write("one\n")
        out = updater.tail(100)
        self.assertEqual(out["text"], "")
        self.assertEqual(out["next"], 4)

    def test_a_missing_log_is_an_empty_answer(self):
        out = updater.tail(0)
        self.assertEqual((out["text"], out["next"], out["size"]), ("", 0, 0))

    def test_a_bogus_offset_starts_from_the_beginning(self):
        self.write("one\n")
        self.assertEqual(updater.tail("nonsense")["text"], "one\n")

    def test_the_utf8_bom_the_script_writes_is_not_shown(self):
        self.log.write_bytes("\ufeffstart\n".encode("utf-8"))
        self.assertEqual(updater.tail(0)["text"], "start\n")


class Status(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="basalt-updater-test-"))
        self.pids = self.dir / "update.pid"
        self.results = self.dir / "update.result.json"
        self.log = self.dir / "update.log"
        for name, value in (("PID_FILE", self.pids), ("RESULT_FILE", self.results), ("LOG", self.log)):
            patcher = mock.patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        state = mock.patch.object(updater, "state", FakeState())
        state.start()
        self.addCleanup(state.stop)
        self.log.write_text("nothing yet\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, True)

    def write_pid(self, pid: int, target: str = "v0.6.0"):
        self.pids.write_text(json.dumps({"pid": pid, "started": 1000, "target": target}), encoding="utf-8")

    def write_result(self, result: dict):
        self.results.write_text(json.dumps(result), encoding="utf-8")

    def test_nothing_ever_ran(self):
        out = updater.status()
        self.assertFalse(out["running"])
        self.assertIsNone(out["pid"])
        self.assertIsNone(out["result"])
        self.assertEqual(out["log_size"], self.log.stat().st_size)

    def test_a_live_process_is_a_run_in_progress(self):
        self.write_pid(4321)
        with mock.patch.object(updater, "_alive", return_value=True):
            out = updater.status()
        self.assertTrue(out["running"])
        self.assertEqual(out["pid"], 4321)
        self.assertEqual(out["target"], "v0.6.0")

    def test_a_process_that_vanished_without_a_result_says_so(self):
        self.write_pid(4321)
        with mock.patch.object(updater, "_alive", return_value=False):
            out = updater.status()
        self.assertFalse(out["running"])
        self.assertTrue(out["result"]["interrupted"])
        self.assertFalse(out["result"]["ok"])

    def test_a_finished_run_reports_its_result_and_stops_being_running(self):
        self.write_pid(4321)
        self.write_result({"ok": True, "to": "v0.6.0"})
        with mock.patch.object(updater, "_alive", return_value=True):
            out = updater.status()
        self.assertFalse(out["running"])
        self.assertEqual(out["result"], {"ok": True, "to": "v0.6.0"})

    def test_a_corrupt_pid_file_is_not_a_run(self):
        self.pids.write_text("{ not json", encoding="utf-8")
        out = updater.status()
        self.assertFalse(out["running"])
        self.assertIsNone(out["pid"])


class Start(unittest.TestCase):
    """The guards: everything that must stop a run before a process is created."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="basalt-updater-test-"))
        self.script = self.dir / "update.ps1"
        self.script.write_text("# test", encoding="utf-8")
        names = {"PID_FILE": self.dir / "update.pid", "RESULT_FILE": self.dir / "update.result.json",
                 "LOG": self.dir / "update.log", "SCRIPT": self.script}
        for name, value in names.items():
            patcher = mock.patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spawn = mock.Mock(return_value=(4242, None))
        spawn = mock.patch.object(updater, "_wmi_create", self.spawn)
        spawn.start()
        self.addCleanup(spawn.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, True)

    def attempt(self, target=None, *, answers=None, repo=None, state=None):
        git = FakeVersion(answers or FETCH_OK, repo=repo)
        st = state or FakeState()
        with mock.patch.object(updater, "version", git), mock.patch.object(updater, "state", st):
            return updater.start(target)

    def test_a_dirty_tree_is_refused(self):
        out = self.attempt(repo={**GIT, "dirty": True})
        self.assertFalse(out["started"])
        self.assertIn("uncommitted changes", out["error"])
        self.spawn.assert_not_called()

    def test_a_non_git_install_is_refused(self):
        out = self.attempt(repo={"git": False})
        self.assertFalse(out["started"])
        self.assertIn("not a git checkout", out["error"])
        self.spawn.assert_not_called()

    def test_a_missing_script_is_refused(self):
        self.script.unlink()
        out = self.attempt()
        self.assertFalse(out["started"])
        self.assertIn("update.ps1", out["error"])
        self.spawn.assert_not_called()

    def test_a_target_must_look_like_a_release_tag(self):
        for target in ("0.6", "v0.6", "local-0.6.0", "v0.6.0/../evil"):
            out = self.attempt(target)
            self.assertFalse(out["started"], target)
            self.assertIn("release tag", out["error"])
        self.spawn.assert_not_called()

    def test_two_runs_at_once_are_refused(self):
        updater.PID_FILE.write_text(json.dumps({"pid": 111, "started": 1}), encoding="utf-8")
        with mock.patch.object(updater, "_alive", return_value=True):
            out = self.attempt()
        self.assertFalse(out["started"])
        self.assertIn("already running", out["error"])
        self.spawn.assert_not_called()

    def test_a_run_starts_and_is_written_down_at_once(self):
        updater.LOG.write_text("an old run\n", encoding="utf-8")
        out = self.attempt("v0.6.0")
        self.assertTrue(out["started"], out)
        self.assertEqual(out["pid"], 4242)
        self.assertEqual(out["target"], "v0.6.0")
        written = json.loads(updater.PID_FILE.read_text(encoding="utf-8"))
        self.assertEqual(written["pid"], 4242)
        self.assertEqual(written["target"], "v0.6.0")
        self.assertFalse(updater.LOG.exists())                     # the log starts clean
        self.spawn.assert_called_once()
        cmdline = self.spawn.call_args.args[0]
        self.assertIn(str(self.script), cmdline)
        self.assertIn("-Target v0.6.0", cmdline)
        self.assertIn("powershell", cmdline.lower())

    def test_a_targetless_run_omits_the_argument(self):
        self.attempt(None)
        self.assertNotIn("-Target", self.spawn.call_args.args[0])

    def test_a_spawn_failure_is_reported_as_an_error(self):
        self.spawn.return_value = (None, "WMI Create returned 21")
        out = self.attempt()
        self.assertFalse(out["started"])
        self.assertEqual(out["error"], "WMI Create returned 21")
        self.assertFalse(updater.PID_FILE.exists())


if __name__ == "__main__":
    unittest.main()
