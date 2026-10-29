"""Run with python3 -m unittest discover -s tests -v from the skill directory.

Git and Treehouse acquisition are real. GitHub responses and successful process
termination are isolated boundaries, so tests never touch live PRs or processes.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/workflow.py"
SPEC = importlib.util.spec_from_file_location("workflow", SCRIPT)
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="treehouse-workflow-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "origin.git"
        self.repo.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "TREEHOUSE_ROOT": str(self.root / "pools"),
            "TREEHOUSE_FEATURES_STATE_DIR": str(self.root / "state"),
            "TREEHOUSE_NO_UPDATE_CHECK": "1",
        })
        self.env.start()
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("init", "--initial-branch=main")
        (self.repo / "README.md").write_text("Fixture\n")
        (self.repo / ".gitignore").write_text("node_modules/\n.env\n")
        (self.repo / "treehouse.toml").write_text("max_trees = 2\nunique_leaf = true\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial fixture")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main")
        self.identity = w.repository(self.repo)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", "-c", "user.name=Workflow Test",
                                 "-c", "user.email=workflow@example.invalid", *args],
                                cwd=cwd or self.repo, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def start(self, branch="feat/one"):
        with w.database() as db:
            return copy.deepcopy(w.start(db, self.identity, branch, "main"))

    def merged(self, item, squash=True):
        path = Path(item["path"])
        (path / "feature.txt").write_text("Feature\n")
        self.git("add", "feature.txt", cwd=path)
        self.git("commit", "-m", "Implement feature", cwd=path)
        head = self.git("rev-parse", "HEAD", cwd=path)
        self.git("push", "origin", item["branch"], cwd=path)
        if squash:
            self.git("merge", "--squash", item["branch"])
            self.git("commit", "-m", "Squash feature")
        else:
            self.git("merge", "--no-ff", item["branch"], "-m", "Merge feature")
        self.git("push", "origin", "main")
        pr = {"number": 7, "url": "https://github.com/example/project/pull/7",
              "state": "MERGED", "mergedAt": "2026-10-29T12:00:00Z",
              "baseRefName": "main", "headRefName": item["branch"],
              "headRefOid": head, "isCrossRepository": False,
              "mergeCommit": {"oid": self.git("rev-parse", "HEAD")}}
        with w.database() as db:
            record = db["features"][item["lease_id"]]
            record.update(pr=7, github_repo="https://github.com/example/project")
            w.save(db)
        return pr

    def complete(self, item, pr):
        with mock.patch.object(w, "pull_request", return_value=("https://github.com/example/project", pr)):
            with w.database() as db:
                return w.finish(db, db["features"][item["lease_id"]])

    def fake_return(self, item):
        original = w.run

        def run(argv, cwd, check=True):
            if argv[:2] == ["treehouse", "return"]:
                self.assertIn(item["lease_id"], argv)
                self.assertNotIn("--force", argv)
                self.git("checkout", "--detach", "origin/main", cwd=Path(item["path"]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original(argv, cwd, check)
        return mock.patch.object(w, "run", side_effect=run)

    def assert_retained(self, item):
        with w.database() as db:
            self.assertIn(item["lease_id"], db["features"])
        self.assertIsNotNone(w.branch_head(item))
        self.assertTrue(w.owns(item, w.current_slot(item)))

    def test_real_acquisition_exact_remote_base_and_durable_lease(self):
        item = self.start()
        self.assertEqual(self.git("rev-parse", "origin/main"),
                         self.git("rev-parse", "HEAD", cwd=Path(item["path"])))
        self.assertTrue(w.owns(item, w.current_slot(item)))
        self.assertEqual(w.repository(item["path"])["repo_id"], self.identity["repo_id"])

    def test_resume_keeps_dirty_work_and_same_reservation(self):
        item = self.start()
        pending = Path(item["path"]) / "pending.txt"
        pending.write_text("Unfinished\n")
        resumed = self.start()
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["lease_id"], item["lease_id"])
        self.assertEqual(pending.read_text(), "Unfinished\n")

    def test_full_pool_does_not_create_third_branch(self):
        self.start("feat/one")
        self.start("feat/two")
        with self.assertRaises(w.WorkflowError):
            self.start("feat/three")
        self.assertEqual(2, len(w.output_json(["treehouse", "status", "--json"], self.repo)))
        self.assertNotIn("feat/three", self.git("branch", "--format=%(refname:short)"))

    def test_concurrent_allocations_get_distinct_slots_and_ports(self):
        def allocate(branch):
            return subprocess.run(["python3", str(SCRIPT), "start", "--repo", str(self.repo),
                                   "--branch", branch], capture_output=True, text=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(allocate, ["feat/one", "feat/two"]))
        self.assertEqual([0, 0], [r.returncode for r in results], [r.stderr for r in results])
        items = [json.loads(r.stdout) for r in results]
        self.assertNotEqual(items[0]["path"], items[1]["path"])
        self.assertTrue(set(items[0]["ports"].values()).isdisjoint(items[1]["ports"].values()))

    def test_unpublished_local_base_is_refused(self):
        (self.repo / "unpublished.txt").write_text("Private base change\n")
        self.git("add", ".")
        self.git("commit", "-m", "Unpublished main change")
        with self.assertRaisesRegex(w.WorkflowError, "ahead of origin"):
            self.start()
        with w.database() as db:
            self.assertEqual({}, db["features"])

    def test_pending_pr_keeps_slot(self):
        item = self.start()
        pr = self.merged(item)
        pr.update(state="OPEN", mergedAt=None)
        self.assertEqual("pending", self.complete(item, pr)["status"])
        self.assert_retained(item)

    def test_squash_merge_completion_deletes_only_verified_feature(self):
        item = self.start()
        pr = self.merged(item)
        dependencies = Path(item["path"]) / "node_modules"
        dependencies.mkdir()
        (dependencies / "cached.txt").write_text("Keep installed dependency\n")
        with self.fake_return(item):
            result = self.complete(item, pr)
        self.assertEqual("finished", result["status"])
        self.assertIsNone(w.branch_head(item))
        self.assertEqual("Keep installed dependency\n", (dependencies / "cached.txt").read_text())
        self.assertEqual(self.git("rev-parse", "origin/main"), self.git("rev-parse", "main"))
        with w.database() as db:
            self.assertEqual({}, db["features"])

    def test_regular_merge_completion(self):
        item = self.start()
        pr = self.merged(item, squash=False)
        with self.fake_return(item):
            self.assertEqual("finished", self.complete(item, pr)["status"])

    def test_dirty_merged_checkout_is_not_returned(self):
        item = self.start()
        pr = self.merged(item)
        (Path(item["path"]) / "uncommitted.txt").write_text("Do not lose me\n")
        with self.assertRaisesRegex(w.WorkflowError, "uncommitted"):
            self.complete(item, pr)
        self.assert_retained(item)

    def test_new_local_commit_after_merge_is_preserved(self):
        item = self.start()
        pr = self.merged(item)
        (Path(item["path"]) / "later.txt").write_text("Post-PR work\n")
        self.git("add", ".", cwd=Path(item["path"]))
        self.git("commit", "-m", "Additional local work", cwd=Path(item["path"]))
        with self.assertRaisesRegex(w.WorkflowError, "absent from the merged PR"):
            self.complete(item, pr)
        self.assert_retained(item)

    def test_missing_remote_merge_commit_blocks_release(self):
        item = self.start()
        pr = self.merged(item)
        pr["mergeCommit"]["oid"] = "f" * 40
        with self.assertRaisesRegex(w.WorkflowError, "not proven present"):
            self.complete(item, pr)
        self.assert_retained(item)

    def test_stale_lease_cannot_release_new_task(self):
        item = self.start()
        pr = self.merged(item)
        with mock.patch.object(w, "current_slot", return_value={"lease_id": "new-owner"}):
            with self.assertRaisesRegex(w.WorkflowError, "Lease identity changed"):
                self.complete(item, pr)
        self.assert_retained(item)

    def test_failed_treehouse_return_does_not_delete_branch(self):
        item = self.start()
        pr = self.merged(item)
        original = w.run

        def run(argv, cwd, check=True):
            if argv[:2] == ["treehouse", "return"]:
                self.git("checkout", "--detach", cwd=Path(item["path"]))
                raise w.WorkflowError("Cannot verify task processes")
            return original(argv, cwd, check)
        with mock.patch.object(w, "run", side_effect=run):
            with self.assertRaisesRegex(w.WorkflowError, "task processes"):
                self.complete(item, pr)
        self.assert_retained(item)
        with self.fake_return(item):
            self.assertEqual("finished", self.complete(item, pr)["status"])

    def test_retry_after_return_does_not_touch_reacquired_checkout(self):
        item = self.start()
        pr = self.merged(item)
        self.git("checkout", "--detach", "origin/main", cwd=Path(item["path"]))
        self.git("checkout", "-b", "feat/next", cwd=Path(item["path"]))
        (Path(item["path"]) / "new-task.txt").write_text("New owner's work\n")
        with w.database() as db:
            db["features"][item["lease_id"]]["phase"] = "returning"
            w.save(db)
        with mock.patch.object(w, "current_slot", return_value={"lease_id": "new-owner"}):
            self.assertEqual("finished", self.complete(item, pr)["status"])
        self.assertEqual("feat/next", self.git("branch", "--show-current", cwd=Path(item["path"])))
        self.assertTrue((Path(item["path"]) / "new-task.txt").exists())

    def test_pr_identity_rejects_wrong_base_repository_and_fork(self):
        item = self.start()
        pr = self.merged(item)
        item["github_repo"] = "https://github.com/example/project"
        for key, value in (("baseRefName", "develop"), ("isCrossRepository", True),
                           ("url", "https://github.com/other/project/pull/7")):
            changed = dict(pr, **{key: value})
            with self.subTest(key=key), mock.patch.object(w, "output_json", return_value=changed):
                with self.assertRaisesRegex(w.WorkflowError, "does not match"):
                    w.pull_request(item, 7)

    def test_track_requires_exact_pushed_head(self):
        item = self.start()
        pr = {"headRefOid": "f" * 40}
        with mock.patch.object(w, "pull_request", return_value=("repo", pr)):
            with w.database() as db, self.assertRaisesRegex(w.WorkflowError, "differs from PR head"):
                w.track(db, db["features"][item["lease_id"]], 7)

    def test_track_and_finish_using_github_json_contract(self):
        item = self.start()
        pr = self.merged(item)
        with w.database() as db:
            record = db["features"][item["lease_id"]]
            del record["pr"]
            del record["github_repo"]
            w.save(db)
        original = w.output_json

        def github_json(argv, cwd):
            if argv[:3] == ["gh", "repo", "view"]:
                return {"url": "https://github.com/example/project"}
            if argv[:3] == ["gh", "pr", "view"]:
                return pr
            return original(argv, cwd)
        with mock.patch.object(w, "output_json", side_effect=github_json):
            with w.database() as db:
                record = w.track(db, db["features"][item["lease_id"]], "7")
                self.assertEqual(7, record["pr"])
            with self.fake_return(item), w.database() as db:
                self.assertEqual("finished", w.finish(db, db["features"][item["lease_id"]])["status"])

    def test_retry_after_branch_deletion_finishes_metadata_cleanup(self):
        item = self.start()
        pr = self.merged(item)
        self.git("checkout", "--detach", "origin/main", cwd=Path(item["path"]))
        self.git("update-ref", "-d", "refs/heads/" + item["branch"], pr["headRefOid"])
        with w.database() as db:
            db["features"][item["lease_id"]]["phase"] = "returned"
            w.save(db)
        with mock.patch.object(w, "current_slot", return_value=None):
            self.assertEqual("finished", self.complete(item, pr)["status"])

    def test_public_output_omits_remote_credentials(self):
        output = w.public_result({"features": [{"origin": "https://token@example.invalid/repo", "path": "checkout"}]})
        self.assertEqual({"features": [{"path": "checkout"}]}, output)

    def test_reconcile_reports_blocked_cleanup_and_keeps_unregistered_tasks(self):
        first = self.start()
        self.start("feat/two")
        self.merged(first)
        with mock.patch.object(w, "pull_request", side_effect=w.WorkflowError("GitHub unavailable")):
            with w.database() as db:
                result = w.reconcile(db, self.identity)
        self.assertEqual({"blocked", "unregistered"}, {r["status"] for r in result["results"]})
        self.assert_retained(first)

    def test_port_assignments_stable_and_distinct_across_repositories(self):
        db = {"port_blocks": {}}
        ports = w.assign_ports(db, "/project-one/slot-1")
        self.assertEqual(ports, w.assign_ports(db, "/project-one/slot-1"))
        self.assertTrue(set(ports.values()).isdisjoint(
            w.assign_ports(db, "/project-two/slot-1").values()))

    def test_port_permission_failure_is_not_reported_as_free(self):
        fake = mock.Mock()
        fake.bind.side_effect = PermissionError(1, "Operation not permitted")
        with mock.patch.object(socket, "socket", return_value=fake):
            with self.assertRaisesRegex(w.WorkflowError, "does not permit checking"):
                w.verify_ports({"web": 3100})

    def test_corrupted_state_is_not_overwritten(self):
        root = w.state_root()
        root.mkdir()
        (root / "state.json").write_text("broken")
        with self.assertRaisesRegex(w.WorkflowError, "Invalid workflow state"):
            with w.database():
                pass
        self.assertEqual("broken", (root / "state.json").read_text())


if __name__ == "__main__":
    unittest.main()
