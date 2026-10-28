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

A killed worker leaves its workspace behind; cleanup runs in Python and does
not run on `SIGKILL`. The directory name identifies its source, so you no
longer have to search every candidate repository to find the owner:

```text
runforge-worker-repo_A-865dc691-20260730T181530Z-p4127-a13f
runforge-worker-source_A-nongit-4f82a1c0-20260730T182011Z-p4198-c82d
```

Reading left to right: the repository or source-directory name, the commit
or content digest (first eight characters), when the worker started (UTC),
its process ID, and a nonce distinguishing simultaneous workers from the
same source. A `-nongit-` marker after the name means a directory-snapshot
or verified-directory source, not a Git repository.

For the full, untruncated identity — including who claimed the experiment
that built it — read the workspace's manifest:

```bash
runforge workspaces PATH
```

This is read-only and safe to run on a live workspace; it only reports what
the manifest already recorded when the workspace was created, not whether
the worker is still running.

To remove one, list what is actually there first, then confirm exactly which
paths to remove — nothing is ever selected automatically, and in particular
never by age:

```bash
runforge workspaces ROOT
runforge workspaces ROOT --remove PATH
```

The first form only lists; it changes nothing. The second removes exactly
the paths passed to `--remove`, which must be among the ones just listed —
a typo'd or unrelated path is rejected rather than silently ignored. A
Git-sourced workspace is deregistered as a worktree (`git worktree remove`
under the hood), not just deleted, so the repository's own worktree list
stays accurate; a non-Git workspace is a plain materialized copy with no
registration to clean up.

Set `RUNFORGE_WORKSPACE_ROOT` (see [the CLI reference](../reference/cli.md) and
[Shared filesystems](shared-filesystems.md)) so `worker-cleanup` (and
`ROOT` above) has one place to scan, instead of searching beside each
repository and the host's temporary directory separately.
