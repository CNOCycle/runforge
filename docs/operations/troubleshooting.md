# Troubleshooting

Symptoms, what causes them, and the supported fix.

## "Experiment is already claimed"

A live worker owns it, or a dead one left its claim. The message names the
holder and when it was acquired.

Confirm that both the worker and its child process have stopped before using
`runforge retry --force EXPERIMENT_DIR` on a failed or `inprogress` experiment.
Forced retry does not clear a claim on a `created` or `init` experiment; preserve
that record and investigate the claim. See the
[recovery workflow](../guides/workers-and-recovery.md#restart-confirmed-interrupted-experiments)
and, for scheduler or container owners, [Slurm and containers](slurm-and-containers.md).

## An experiment is stuck in `inprogress`

It may still be running, or its worker may have died. Nothing recovers an
interrupted run automatically; see
[Worker lifecycle and claims](../architecture/worker-lifecycle-and-claims.md).
Use `retry --force` only after confirming both worker and child have stopped.

## `run` exits 2 but everything looks fine

Some candidate's metadata may be invalid. `discover` reports each invalid
candidate with its path and a reason. Exit `2` can also indicate a CLI or
discovery error; inspect the diagnostic and execution summary. Valid plans may
already have run.

## The program created a directory literally named `{ARTIFACT_DIR}`

A planner placeholder was written inside a wrapper script, where nothing
substitutes it. Use `$RUNFORGE_ARTIFACT_DIR` inside scripts and `{ARTIFACT_DIR}`
only on the RunForge command line. See
[Dynamic environment variables](../guides/dynamic-environment-variables.md).

## "command.arguments must be an array of non-empty strings"

Your shell expanded an unset variable to nothing before RunForge saw it —
usually `"$RUNFORGE_ARTIFACT_DIR"` on the command line. Use `'{ARTIFACT_DIR}'`
in single quotes.

## The command fails in the worker but works locally

It probably depends on an untracked file. `current-head` records untracked names
but not contents, and warns at planning time. Commit or stage the file. See
[Source modes](../reference/source-modes.md).

## "Verified-directory source has changed since planning"

The live directory no longer matches the manifest captured at planning. This is
the mode working as intended. Either restore the directory, or plan again to
capture the new state — and consider `directory-snapshot` if the source changes
while experiments are queued.

## A matrix mapping is missing

Planning warns and still exits `0` if the artifact cannot be written, because
the plans themselves are published and runnable. The mapping is derived from
each `config.json`, so nothing is lost permanently. Check disk space and
permissions on the report root.

## Leftover `.workspace/` directories

Normal execution removes the temporary `.workspace/` inside the experiment after
each command, including ordinary command failures. An abrupt kill, crash, or
cleanup error can leave it behind. Its contents do not provide process resume
or reattachment. The next execution of that experiment, normally
`runforge retry --force` after confirming the worker and child stopped, deletes
it and builds a fresh one, so copy out anything you need first. A leftover Git
worktree you locked with `git worktree lock` is never deleted: execution fails
until you unlock it. Host/PID/owner
records and age alone are not proof that the process stopped. See
[workers and recovery](../guides/workers-and-recovery.md#temporary-workdir).
