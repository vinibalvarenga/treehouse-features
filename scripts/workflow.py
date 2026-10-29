#!/usr/bin/env python3
"""Feature metadata and verified PR cleanup; Treehouse owns the worktree pool."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
import uuid


class WorkflowError(Exception):
    pass


def run(argv, cwd, check=True):
    executable = shutil.which(argv[0])
    if not executable:
        fallback = Path.home() / ".local/bin" / argv[0]
        if fallback.is_file() and os.access(fallback, os.X_OK):
            executable = str(fallback)
    if not executable:
        raise WorkflowError("Required command is not installed: " + argv[0])
    env = dict(os.environ, TREEHOUSE_NO_UPDATE_CHECK="1", GH_PROMPT_DISABLED="1",
               GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run([executable, *argv[1:]], cwd=str(cwd), env=env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("Command timed out; inspect existing leases before retrying: "
                            + argv[0]) from exc
    if check and result.returncode:
        raise WorkflowError((result.stderr or result.stdout or
                             "Command failed: " + argv[0]).strip())
    return result


def git(repo, *args, check=True):
    return run(["git", *args], repo, check=check)


def output_json(argv, cwd):
    result = run(argv, cwd)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("Invalid JSON from " + argv[0]
                            + "; inspect its state before retrying") from exc


def repository(path):
    common = Path(git(path, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").stdout.strip()).resolve()
    if common.name != ".git" or not common.is_dir():
        raise WorkflowError("This workflow requires a normal Git checkout, not a bare repository")
    repo = common.parent
    origin = git(repo, "remote", "get-url", "origin").stdout.strip()
    key = hashlib.sha256(str(common).encode()).hexdigest()
    return {"repo": str(repo), "common_dir": str(common), "origin": origin, "repo_id": key}


def state_root():
    return Path(os.environ.get("TREEHOUSE_FEATURES_STATE_DIR",
                               str(Path.home() / ".local/state/treehouse-features"))).expanduser()


def save(db):
    root = state_root()
    descriptor, name = tempfile.mkstemp(prefix=".state-", dir=str(root))
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(db, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, root / "state.json")
        parent = os.open(str(root), os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def database():
    root = state_root()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (root / "state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            path = root / "state.json"
            db = json.loads(path.read_text()) if path.exists() else {
                "version": 1, "features": {}, "port_blocks": {}}
            if (db.get("version") != 1 or not isinstance(db.get("features"), dict)
                    or not isinstance(db.get("port_blocks"), dict)):
                raise WorkflowError("Unsupported workflow state; refusing to overwrite it")
            yield db
        except json.JSONDecodeError as exc:
            raise WorkflowError("Invalid workflow state; refusing to overwrite it") from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def entries(db, repo):
    return [item for item in db["features"].values() if item["repo_id"] == repo["repo_id"]]


def feature(db, repo, lease_id):
    item = db["features"].get(lease_id)
    if not item or item["repo_id"] != repo["repo_id"]:
        raise WorkflowError("No registered feature for that lease in this repository")
    if item["origin"] != repo["origin"]:
        raise WorkflowError("Repository origin changed; keeping the reservation and branch")
    return item


def current_slot(item):
    slots = output_json(["treehouse", "status", "--json"], item["repo"])
    if not isinstance(slots, list):
        raise WorkflowError("Unexpected Treehouse status format")
    return next((slot for slot in slots if Path(slot["path"]).resolve()
                 == Path(item["path"]).resolve()), None)


def owns(item, slot):
    return (slot is not None and slot.get("lease_id") == item["lease_id"]
            and slot.get("lease_holder") == item["lease_holder"])


def branch_head(item):
    result = git(item["repo"], "show-ref", "--verify", "--quiet",
                 "refs/heads/" + item["branch"], check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise WorkflowError(result.stderr.strip())
    return git(item["repo"], "rev-parse", "--verify",
               "refs/heads/" + item["branch"] + "^{commit}").stdout.strip()


def verify_checkout(item, clean=False, allow_detached=False):
    result = git(item["path"], "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    detached = result.returncode == 1 and not result.stdout.strip()
    if result.returncode not in (0, 1):
        raise WorkflowError("Cannot determine checkout branch; keeping the reservation")
    if result.stdout.strip() != item["branch"] and not (allow_detached and detached):
        raise WorkflowError("Checkout is on another branch; keeping the reservation")
    if clean and git(item["path"], "status", "--porcelain",
                     "--untracked-files=all").stdout:
        raise WorkflowError("Checkout has uncommitted or untracked changes; keeping the reservation")
    return git(item["path"], "rev-parse", "HEAD").stdout.strip()


def assign_ports(db, path):
    blocks = db["port_blocks"]
    if path not in blocks:
        occupied = set(blocks.values())
        base = next((n for n in range(3100, 65000, 10) if n not in occupied), None)
        if base is None:
            raise WorkflowError("No free port blocks in the configured range")
        blocks[path] = base
    base = blocks[path]
    return {"web": base, "api": base + 1, "aux": base + 2}


def verify_ports(ports):
    held = []
    try:
        for port in ports.values():
            for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                sock = socket.socket(family, socket.SOCK_STREAM)
                held.append(sock)
                try:
                    if family == socket.AF_INET6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind((address, port))
                except OSError as exc:
                    if family == socket.AF_INET6 and exc.errno in (
                            errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                        continue
                    if exc.errno in (errno.EPERM, errno.EACCES):
                        raise WorkflowError("This execution environment does not permit checking local ports; "
                                            "use an authorized terminal before starting services") from exc
                    raise WorkflowError("Port %s is unavailable; keeping the lease for inspection" % port) from exc
    finally:
        for sock in held:
            sock.close()


def start(db, repo, branch, base):
    for item in entries(db, repo):
        if item["branch"] == branch:
            feature(db, repo, item["lease_id"])
            if item["base"] != base or item["phase"] not in ("preparing", "active"):
                raise WorkflowError("Existing task has a different base or incomplete cleanup; inspect status")
            if not owns(item, current_slot(item)):
                raise WorkflowError("Existing task no longer owns its lease; inspect status")
            head = verify_checkout(item)
            if item["phase"] == "preparing":
                if head != item["base_commit"]:
                    raise WorkflowError("Acquired HEAD differs from fetched base; inspect the retained lease")
                item["phase"] = "active"
                save(db)
            return dict(item, resumed=True)
    git(repo["repo"], "check-ref-format", "--branch", branch)
    git(repo["repo"], "check-ref-format", "--branch", base)
    if branch == base:
        raise WorkflowError("Feature branch must differ from its base")
    git(repo["repo"], "fetch", "origin")
    remote_ref = "refs/remotes/origin/" + base
    remote_head = git(repo["repo"], "rev-parse", "--verify", remote_ref + "^{commit}").stdout.strip()
    local = git(repo["repo"], "show-ref", "--verify", "--hash", "refs/heads/" + base, check=False)
    if local.returncode == 0 and local.stdout.strip() != remote_head:
        ahead = git(repo["repo"], "merge-base", "--is-ancestor", remote_head,
                    "refs/heads/" + base, check=False)
        if ahead.returncode == 0:
            raise WorkflowError("Local base is ahead of origin; refusing to start from unpublished commits")
        if ahead.returncode != 1:
            raise WorkflowError("Cannot verify local and remote base ancestry")
    holder = "feature:" + str(uuid.uuid4())
    allocation = output_json(["treehouse", "get", "--lease", "--json", "--no-fetch",
                              "--base", base, "--branch", branch, "--lease-holder", holder], repo["repo"])
    if not allocation.get("lease_id") or not allocation.get("path"):
        raise WorkflowError("Treehouse returned no lease identity; inspect treehouse status before retrying")
    item = dict(repo, path=str(Path(allocation["path"]).resolve()),
                lease_id=allocation["lease_id"], lease_holder=holder, branch=branch,
                base=base, base_commit=remote_head, phase="preparing",
                created_at=datetime.now(timezone.utc).isoformat())
    db["features"][item["lease_id"]] = item
    item["ports"] = assign_ports(db, item["path"])
    save(db)
    if verify_checkout(item) != remote_head:
        raise WorkflowError("Acquired HEAD differs from fetched origin base; lease retained for inspection")
    item["phase"] = "active"
    save(db)
    return dict(item, resumed=False)


PR_FIELDS = "number,url,state,mergedAt,baseRefName,headRefName,headRefOid,isCrossRepository,mergeCommit"


def pull_request(item, spec=None):
    gh_repo = item.get("github_repo")
    if not gh_repo:
        gh_repo = output_json(["gh", "repo", "view", "--json", "url"], item["repo"])["url"]
    pr = output_json(["gh", "pr", "view", str(spec if spec is not None else item["pr"]),
                      "--repo", gh_repo, "--json", PR_FIELDS], item["repo"])
    expected = urlparse(gh_repo)
    actual = urlparse(pr["url"])
    if (actual.netloc.lower() != expected.netloc.lower()
            or actual.path.rsplit("/pull/", 1)[0].rstrip("/").lower()
            != expected.path.rstrip("/").lower()
            or pr["isCrossRepository"]
            or pr["baseRefName"] != item["base"]
            or pr["headRefName"] != item["branch"]):
        raise WorkflowError("PR does not match this repository, feature branch, and base; keeping the lease")
    return gh_repo, pr


def track(db, item, spec):
    if item["phase"] != "active" or not owns(item, current_slot(item)):
        raise WorkflowError("Cannot register a PR without the task's active lease")
    head = verify_checkout(item, clean=True)
    gh_repo, pr = pull_request(item, spec)
    if head != pr["headRefOid"]:
        raise WorkflowError("Local commit differs from PR head; push/verify the intended work first")
    item.update(github_repo=gh_repo, pr=pr["number"], pr_url=pr["url"])
    save(db)
    return item


def finish(db, item):
    if not item.get("pr"):
        raise WorkflowError("No PR registered; keeping the lease")
    _, pr = pull_request(item)
    if pr["state"] != "MERGED" or not pr["mergedAt"]:
        return {"lease_id": item["lease_id"], "status": "pending", "pr_url": pr["url"]}
    expected = pr["headRefOid"]
    local = branch_head(item)
    if local is None and item["phase"] not in ("returning", "returned"):
        raise WorkflowError("Feature branch disappeared before return; keeping the lease")
    if local is not None and local != expected:
        raise WorkflowError("Local feature has commits absent from the merged PR; keeping branch and lease")
    git(item["repo"], "fetch", "origin")
    merge_commit = (pr.get("mergeCommit") or {}).get("oid")
    if not merge_commit:
        raise WorkflowError("Cannot verify PR merge commit; keeping the reservation")
    merged = git(item["repo"], "merge-base", "--is-ancestor", merge_commit,
                 "refs/remotes/origin/" + item["base"], check=False)
    if merged.returncode:
        raise WorkflowError("PR merge commit is not proven present in the fetched remote base")
    slot = current_slot(item)
    if owns(item, slot):
        if verify_checkout(item, clean=True, allow_detached=item["phase"] == "returning") != expected:
            raise WorkflowError("Checkout commit differs from merged PR; keeping the lease")
        item["phase"] = "returning"
        save(db)
        run(["treehouse", "return", "--if-lease-id", item["lease_id"],
             "--if-lease-holder", item["lease_holder"], item["path"]], item["repo"])
        item["phase"] = "returned"
        save(db)
    elif item["phase"] not in ("returning", "returned"):
        raise WorkflowError("Lease identity changed; refusing to touch another task's worktree")
    # A retry after successful return may see a new lease at the same path.
    # Never touch that checkout; delete only the old, verified feature ref.
    ref = "refs/heads/" + item["branch"]
    worktrees = git(item["repo"], "worktree", "list", "--porcelain", "-z").stdout
    if "branch " + ref in worktrees.split("\0"):
        raise WorkflowError("Feature branch is checked out in a worktree; local branch retained")
    if branch_head(item) is not None:
        git(item["repo"], "update-ref", "-d", ref, expected)
    # Retry config cleanup even after an earlier run already deleted the ref.
    result = git(item["repo"], "config", "--local", "--remove-section",
                 "branch." + item["branch"], check=False)
    if result.returncode not in (0, 128):
        raise WorkflowError("Branch deleted but its configuration could not be cleaned; retry finalization")
    del db["features"][item["lease_id"]]
    save(db)
    return {"lease_id": item["lease_id"], "status": "finished", "branch": item["branch"],
            "path": item["path"], "pr_url": pr["url"]}


def reconcile(db, repo):
    results = []
    for item in list(entries(db, repo)):
        try:
            feature(db, repo, item["lease_id"])
            results.append(finish(db, item) if item.get("pr") else {
                "lease_id": item["lease_id"], "status": "unregistered", "branch": item["branch"]})
        except WorkflowError as exc:
            results.append({"lease_id": item["lease_id"], "status": "blocked", "error": str(exc)})
    return {"results": results}


def public_result(value):
    # A remote URL can contain credentials; keep it only in the private state.
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items() if key != "origin"}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "track", "finish", "reconcile", "status", "check-ports"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True, type=Path)
        if name == "start":
            command.add_argument("--branch", required=True)
            command.add_argument("--base", default="main")
        if name in ("track", "finish", "check-ports"):
            command.add_argument("--lease-id", required=True)
        if name == "track":
            command.add_argument("--pr", required=True)
    args = parser.parse_args()
    try:
        repo = repository(args.repo)
        with database() as db:
            if args.command == "start":
                result = start(db, repo, args.branch, args.base)
            elif args.command == "status":
                result = {"features": entries(db, repo)}
            elif args.command == "reconcile":
                result = reconcile(db, repo)
            else:
                item = feature(db, repo, args.lease_id)
                if args.command == "track":
                    result = track(db, item, args.pr)
                elif args.command == "check-ports":
                    if not owns(item, current_slot(item)):
                        raise WorkflowError("Cannot check ports for a lease belonging to another task")
                    verify_ports(item["ports"])
                    result = {"lease_id": item["lease_id"], "ports": item["ports"], "status": "available"}
                else:
                    result = finish(db, item)
        print(json.dumps(public_result(result), indent=2, sort_keys=True))
        if args.command == "reconcile" and any(r["status"] == "blocked" for r in result["results"]):
            return 2
        return 0
    except (WorkflowError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
