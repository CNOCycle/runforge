# Troubleshooting

Symptoms, what causes them, and the supported fix.

## "Experiment is already claimed"

A live worker owns it, or a dead one left its claim. The message names the
holder and when it was acquired.

Confirm the process has stopped, then `runforge retry --force EXPERIMENT_DIR`.
If the owner is not resolvable from where you are, see
[Slurm and containers](slurm-and-containers.md).

## An experiment is stuck in `inprogress`

Its worker died. Nothing recovers this automatically by design; see
[Worker lifecycle and claims](../architecture/worker-lifecycle-and-claims.md).
Use `retry --force` after confirming the process is gone.

## `worker` exits 2 but everything looks fine

Some candidate's metadata is invalid. `discover` reports each invalid candidate
with its path and a reason. Valid plans still ran — `2` means the scan is
untrustworthy, not that nothing happened.

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

## Leftover `runforge-worker-*` directories

A killed worker leaves its temporary worktree behind; `TemporaryDirectory`
cleanup does not run on `SIGKILL`. Find which repository owns one with
`git worktree list` in each candidate repository, then remove it with
`git worktree remove --force PATH`.

Note that `git worktree prune` does **not** help while the directory still
exists — it only clears registrations whose directory is already gone.
