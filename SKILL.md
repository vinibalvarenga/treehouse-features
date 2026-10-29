---
name: treehouse-features
description: Start, resume, and finish feature development in reusable Treehouse worktrees, and reconcile merged PRs before the next coding task. Use automatically at the start and end of feature or bug-fix work in a Git repository, whether the terminal is Herdr, tmux, or a plain shell. Do not allocate worktrees for read-only research, reviews, or machine configuration.
---

# Treehouse feature workflow

Use Treehouse as the sole owner of pool acquisition, reservations, resets, and returns.
The bundled helper only associates a feature with its lease, PR, and stable ports and verifies completion.
It needs Python 3.9+, `git`, `treehouse`, and authenticated `gh` for PR operations.
It also checks `~/.local/bin` for installed commands.
Use a short global instruction to load this skill at task start and completion; supported agents can also discover its installed skill directory.

## Start or resume

Before editing repository files, read the repository instructions and identify the intended base branch.
Use `main` unless the project or user specifies another base.
Use the absolute main checkout path as REPO and resolve HELPER to `scripts/workflow.py` next to this `SKILL.md` in its actual installed directory.

1. Run `python3 HELPER reconcile --repo REPO` to finalize registered features whose PRs have merged.
   Report blocked cleanup for the affected feature and preserve its worktree and branch rather than forcing cleanup.
2. Run `python3 HELPER start --repo REPO --branch FEATURE --base BASE`.
   Save the returned `lease_id`, `path`, and `ports` in task context.
   Repeating start for the same registered branch resumes its lease and preserves changes.
   When already inside a registered feature checkout, resume that task instead of nesting another allocation.
   A full pool is a stopping condition: do not enlarge the pool or edit the main checkout as a fallback.
3. Work only inside the acquired path and read its applicable instructions.
   Explicitly set tool working directories; a `cd` in another terminal does not relocate an existing agent or sandbox.
   If the checkout is outside the agent's writable scope, use an authorized session rooted there or explain the actual constraint before editing.
4. Prepare and verify the project using its existing package manager, lockfile, runtime, and local-development instructions.
   Synchronize dependencies with the checked-out lockfile while reusing installed files.
   Verify preparation explicitly: Treehouse hook failures do not fail acquisition.
   Do not change infrastructure or install a global startup hook to make a checkout run.

Use `python3 HELPER status --repo REPO` to inspect task records.
After a partially failed acquisition, inspect helper status and `treehouse status --json` before retrying; retain the existing lease instead of allocating another blindly.

## Terminals and local services

Read [references/terminals.md](references/terminals.md) only when launching a separate terminal or agent.
Herdr and tmux are presentation adapters; retain the existing workspace or session and use each acquired path as its tab, window, or pane directory.
Do not use their worktree-creation or checkout-removal commands for Treehouse-owned slots.

The helper assigns a stable three-port block per checkout across all repositories: `web`, `api`, and `aux`.
Map only the ports the application needs to its supported CLI flags or process environment, including frontend/backend and callback URLs.
Before starting services, run `python3 HELPER check-ports --repo REPO --lease-id LEASE`.
Port assignments are reservations within this workflow, not permanent operating-system locks; configure servers to fail on collisions instead of auto-selecting a different port.
Do not overwrite tracked `.env` files or silently choose another port when one is occupied.
Keep task servers attributable to their terminal and verify the expected listener and health endpoint before testing.
Worktrees do not isolate databases, containers, or cloud resources; follow existing project isolation rules and stop before a conflicting shared migration.

## PR and completion

Develop, review, and verify the feature according to the request and project instructions.
Use the normal PR process and never create a draft PR.
This skill does not independently authorize pushing, opening or merging PRs, publishing, or deploying.

After an authorized PR has been opened or identified, run `python3 HELPER track --repo REPO --lease-id LEASE --pr NUMBER_OR_URL`.
Before ending the task, run `python3 HELPER finish --repo REPO --lease-id LEASE` if its PR has merged, or `reconcile --repo REPO` to check registered features.
A PR awaiting review keeps its lease even when the agent exits.
If pushing or opening a PR is not authorized, retain the lease and report the saved path and branch.

Completion verifies the PR repository, merged state, destination branch, merged commit in the fetched remote base, and exact feature commit, including squash merges.
It refuses dirty checkouts, commits absent from the PR, and mismatched active leases.
Treehouse then terminates task processes, detaches/resets the checkout, and returns it with the original `--if-lease-id` condition.
Only after successful return does the helper delete the unchanged local feature ref.
Never replace these checks with `return --force`, `return --all`, `destroy`, or blind `git branch -D`.
Do not write to a returned checkout because another task can acquire it immediately.

Cleanup runs on the next agent invocation and at feature completion, with no background service.
Workflow metadata lives under `~/.local/state/treehouse-features`; this is not a second pool or a task queue.
Use `TREEHOUSE_FEATURES_STATE_DIR` and `TREEHOUSE_ROOT` only for isolated tests or a user-requested alternative configuration.
