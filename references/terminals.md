# Terminal adapters

Use an adapter when the task needs another terminal or agent and the current session permits it.
Keep the terminal manager already in use.
An agent can also work directly in the acquired path without opening another agent.
User restrictions on delegation still apply.

## Herdr

Inside Herdr, create a tab in the existing workspace using the acquired checkout:

```sh
herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$worktree_path" --label "$feature" --no-focus
```

Capture the response's `.result.root_pane.pane_id` if another command needs the terminal.
Use `herdr pane run` for a shell command or the documented `herdr agent start` interface for an authorized agent launch.
Tab creation and pane splitting accept process variables through `--env KEY=VALUE`.
Do not create another workspace merely to represent a worktree or change global Herdr settings.

## tmux

Inside tmux, create a window in the current session rooted at the acquired checkout:

```sh
tmux new-window -d -P -F '#{pane_id}' -n "$feature" -c "$worktree_path"
```

Capture the returned pane ID rather than predicting an index.
Send startup commands only to a shell prompt owned by this task, quoting paths and arguments for its actual shell.
Keep servers attributable to that window and do not change global tmux options.

## Completion

Run finalization from the main checkout when possible, particularly when another process performs it.
Treehouse return terminates processes resident in the target checkout, so keep shared coordinators outside the pool.
After return, close only the task's known tab or window if necessary and preserve its parent workspace or session.
Terminal IDs do not prove a merge or lease ownership.
