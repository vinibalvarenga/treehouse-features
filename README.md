# Treehouse Features

An agent skill for developing features in reusable [Treehouse](https://github.com/kunchenguid/treehouse) worktrees and returning them after their pull requests merge.
It works with Herdr, tmux, and plain terminals.

Treehouse owns the worktree pool, reservations, resets, and returns.
This skill adds feature instructions and a Python helper that records each feature's lease, PR, and stable development ports.

## Workflow

1. Reconcile previously registered PRs when the agent starts its next coding task.
2. Acquire a Treehouse lease and create a feature branch from the intended remote base.
3. Develop and verify the feature inside the acquired checkout, reusing its installed dependencies.
4. Register the feature's PR and keep the lease while review is pending.
5. After merge, verify the PR, commit, checkout, and lease before returning the worktree and deleting its local branch.

Cleanup runs at task completion or on the next agent invocation.
There is no background service or separate task queue.
The skill does not independently authorize pushes, PR creation, merges, deployments, or publishing.

## Requirements

- macOS or Linux with Python 3.9 or later.
- Git and Treehouse with persistent leases, JSON output, `--branch`, and conditional return support; tested with Treehouse v3.1.7.
- An authenticated [GitHub CLI](https://cli.github.com/) for PR registration and verification.
- Herdr or tmux only if you want their terminal adapters.

## Install

For Claude Code, clone this repository into its user skill directory:

```sh
git clone https://github.com/vinibalvarenga/treehouse-features.git \
  ~/.claude/skills/treehouse-features
```

For Codex, use its user skill directory instead:

```sh
git clone https://github.com/vinibalvarenga/treehouse-features.git \
  ~/.agents/skills/treehouse-features
```

Add a short rule to your agent's global instructions so the workflow is followed at both lifecycle boundaries.
Use the actual installed path if it differs from this example:

```markdown
## Feature worktrees

- Before feature or bug-fix edits, read and follow `~/.claude/skills/treehouse-features/SKILL.md`.
  Reconcile merged PRs, then acquire or resume the feature's Treehouse lease.
- Before ending the task, follow the skill's completion flow.
  Keep pending PRs reserved and return a worktree only after verifying the exact feature was merged into its intended remote base.
```

Keep your existing repository instructions and infrastructure settings.
Restart an already-running agent if it has not loaded the updated instructions.

## Configure Treehouse

Set the desired pool limit in your repository's `treehouse.toml` or use the user-level `~/.config/treehouse/config.toml` for defaults:

```toml
max_trees = 5
unique_leaf = true
```

Repository configuration takes precedence over user defaults.
Treehouse creates slots on demand and refuses acquisition when the pool is full or its existing slots cannot be reused.
The helper defaults to a base branch named `main`; the agent should pass the project's actual base when different.
It refuses to start from an unpublished local base that is ahead of the remote base.

## Helper interface

The agent calls `scripts/workflow.py`; you do not need to run it for each feature yourself.
The commands below document the interface for inspection and troubleshooting:

```sh
python3 scripts/workflow.py reconcile --repo /path/to/project
python3 scripts/workflow.py start --repo /path/to/project --branch feat/example --base main
python3 scripts/workflow.py status --repo /path/to/project
python3 scripts/workflow.py check-ports --repo /path/to/project --lease-id LEASE_ID
python3 scripts/workflow.py track --repo /path/to/project --lease-id LEASE_ID --pr PR_NUMBER
python3 scripts/workflow.py finish --repo /path/to/project --lease-id LEASE_ID
```

Successful commands write JSON to stdout and errors write JSON to stderr.
`reconcile` exits with status 2 when any registered cleanup is blocked.
Other failures exit with status 1.

Metadata is stored under `~/.local/state/treehouse-features`.
Stable `web`, `api`, and `aux` ports are assigned per checkout across repositories, starting at 3100 with a ten-port interval between checkouts.
These assignments do not hold operating-system sockets open; check ports before starting services and configure servers to fail on a port collision.
The agent maps the assigned ports to the application's supported flags, environment variables, and URLs.

## Completion checks

Before returning a worktree, the helper verifies:

- The registered PR belongs to the expected repository, feature branch, and base branch.
- The PR is merged and its merge commit is present in the freshly fetched remote base.
- The local feature commit matches the PR head, including squash-merge workflows.
- The checkout has no uncommitted or untracked changes.
- The original lease identity still belongs to this task.

Fork PRs are currently rejected.
A failed process check or return keeps the local branch and workflow record for inspection or retry.
Retries do not return a checkout that has already been acquired by another task.
Local branch deletion uses the verified commit as a compare-and-delete condition.

Worktrees isolate source files, but databases, containers, ports, and cloud resources still need the project's own isolation strategy.
Sandbox restrictions can prevent port checks or Treehouse's process inspection; the workflow reports the restriction and preserves the reservation.

## Tests

Run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The tests use temporary local repositories and the installed Treehouse binary for real acquisition, concurrency, and pool-limit checks.
GitHub responses and successful process termination are simulated so the suite does not modify live PRs or stop user processes.
No runtime Python dependencies are required beyond the standard library.

See [SKILL.md](SKILL.md) for agent instructions and [terminal adapters](references/terminals.md) for Herdr and tmux examples.
