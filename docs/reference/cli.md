# CLI Reference

`runforge SUBCOMMAND --help` lists every option with its semantic default. This
page covers what help output cannot: which command to reach for, how they
interact, and what each exit code means.

## Subcommands

| Command | Effect | Returns |
| --- | --- | --- |
| `plan` | Creates one experiment directory. Never executes. | `0`, or `2` on error |
| `launch` | Plans, then immediately runs in the foreground. | The command's exit code |
| `matrix` | Expands a parameter matrix into many plans. Never executes. | `0`, or `2` on error |
| `matrix-show` | Renders a saved matrix mapping. Read-only. | `0`, or `2` on a bad artifact |
| `run` | Executes one explicit experiment directory. | The command's exit code |
| `retry` | Archives the prior attempt and runs the same plan again. | The command's exit code |
| `discover` | Lists a report root. Read-only. | `0`, or `2` if any metadata is invalid |
| `worker` | Executes available plans from one report root. | See below |
| `worker-inspect` | Renders a worker workspace's manifest. Read-only. | `0`, or `2` on a bad workspace |
| `worker-cleanup` | Lists leftover worker workspaces; removes only those passed to `--remove`. | `0`, or `2` on error |

`worker` exit codes carry more meaning than the rest:

| Code | Meaning |
| --- | --- |
| `0` | Every selected plan succeeded, or nothing was selected |
| `1` | A selected command failed |
| `2` | Some candidate's metadata is invalid |

`2` takes precedence over `1`. It reports that the scan itself is untrustworthy,
so a caller testing for `1` must not assume every candidate was inspected.

## Choosing A Command

**Planning and execution at different times or on different machines** — `plan`
then `run`. This is the reason the two are separate.

**One experiment, now** — `launch`.

**Many experiments** — `matrix` to plan, then one or more `worker` processes to
consume them.

**A specific experiment from a report root** — `run`. It uses the same
claim-aware boundary as `worker`, so it is safe alongside workers.

**Finding out what exists** — `discover`. It never changes anything.

## Shared Options

`--stream-output` / `-s` on `launch`, `run`, `retry`, and `worker` mirrors child
output to the console while still writing `stdout.log` and `stderr.log`.

`--force` / `-f` on `retry` confirms that you have independently verified the
previous process stopped.

`--max-tasks N` / `-n N` on `worker` bounds the number of successfully
completed tasks in one invocation. Contention, stale-after-claim skips, and
failed executions do not consume the completion quota; the worker continues
through its initial snapshot until the quota is reached or no candidates remain.
`N` must be positive.

## The Command After `--`

Everything after `--` is the experiment command. It is recorded as an argument
array and executed without a shell unless `--shell` is given.

Planner placeholders such as `{ARTIFACT_DIR}`, `{INPUT_DIR}`, and matrix
parameter names are substituted into this command at planning time. Quote them
with single quotes so your shell does not interpret the braces. See
[Dynamic environment variables](../guides/dynamic-environment-variables.md) for
the distinction between placeholders and runtime environment variables.

## Effective Arguments

Every executable subcommand prints a block of effective arguments before doing
anything, showing resolved absolute paths, `not set` for unset values, and
`enabled`/`disabled` for booleans. Environment *values* are never printed —
only the names of loaded variables — because they may hold credentials.

## Environment Variables

| Variable | Direction | Purpose |
| --- | --- | --- |
| `RUNFORGE_ARTIFACT_DIR` | Set by the worker | Where the child command should write output |
| `RUNFORGE_INPUT_DIR` | Set by the worker | The rendered immutable input tree |
| `RUNFORGE_CLAIM_OWNER` | Read by RunForge | Identity recorded in a claim, for recovery |
| `RUNFORGE_WORKSPACE_ROOT` | Read by RunForge | Parent directory for the worker's temporary Git worktree or materialized directory-snapshot workspace |

`RUNFORGE_CLAIM_OWNER` and `RUNFORGE_WORKSPACE_ROOT` are the two RunForge reads.
`RUNFORGE_CLAIM_OWNER` appears in operator error messages, so it must not
contain secrets. `RUNFORGE_WORKSPACE_ROOT` must already exist and be writable by
the worker; RunForge does not create it, and an unusable value fails the
experiment with a clear error rather than falling back silently. Unset or
blank, the worker keeps its previous defaults: next to the source repository
for a Git source, or the OS default temporary directory for a
directory-snapshot source.

## Version

```bash
runforge --version
```

Prints the package version, with the first four characters of the Git revision
appended when running from a source checkout. It requires no subcommand and
inspects nothing.
