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

It may still be running, or its worker may have died. RunForge never recovers
an interrupted run on its own; recovery starts only when you run a command. See
[Worker lifecycle and claims](../architecture/worker-lifecycle-and-claims.md).
To list candidates, run `runforge discover --older-than 6h PATH` (or
`--started-before TIME`); it adds a `stale:` line under each `inprogress`
experiment started before the cutoff. That is a hint only and prints no retry
command. When you start `runforge run` or `runforge retry` on a report root, it
first prints a recovery note for each `inprogress` or claimed experiment whose
worker is stale or cannot be verified, then goes on to run the other eligible
experiments. Printing the notes leaves those experiments unchanged. A note shows
the recorded owner and an assessment (stale or unknown) and then, by evidence:
the `runforge retry --force` command when a force retry applies; a `ps` check
instead when the recorded PID is still running; or, for a `created` or `init`
claim, that it cannot be force-retried and needs inspection. A claim verified as
active gets no note.

- Use `retry --force` only after confirming both worker and child have stopped.
  Add `--reuse-artifacts` to copy the previous artifacts back for a program that
  resumes from its own checkpoints; see
  [Reuse artifacts](../guides/workers-and-recovery.md#reuse-artifacts-for-application-checkpoint-recovery).
- On an interruptible machine that has just started and shares its report root
  with no other machine, `runforge onstart --single-node PATH` restarts every
  `inprogress` experiment this way and then runs the remaining plans. See
  [Interruptible machines](interruptible-machines.md).

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
