# Workers, holds, and recovery

## Before starting again after a restart

After a Docker restart, host reboot, or killed job, inspect the previous work
before starting another runner. The report root holds durable experiments;
the workspace root holds temporary source checkouts. Keep reports and any
checkpoints on persistent storage if they must survive container replacement.

run, retry, and launch automatically check existing inprogress or claimed
experiments found in their initial selection, before starting any new attempt.
Each claim is assessed from local evidence:

| Assessment | Meaning | Startup output |
| --- | --- | --- |
| active | The claim's HOSTNAME:PID runs on this host and started before the claim. | Nothing: a concurrent worker is normal. |
| stale | The process exited (including an unreaped zombie), its PID is gone, or the PID was reused by a process started after the claim. | Owner, matching workspace, and the forced-retry command to run after confirmation. |
| unknown | Another host, a custom RUNFORGE_CLAIM_OWNER, a missing or unreadable claim, or a running PID whose start time cannot be read. | Owner and workspace details. A running PID gets a ps check instead of a retry command. |

Process start times come from Linux /proc; elsewhere a running PID is unknown.
The report does not clear claims, change status, retry work, or block unrelated
runnable experiments. Stale means the recorded worker process is gone; a child
it started may still run, so confirm both before recovery. These are
observations, not a guarantee about activity when execution acquires its claim.

Startup reuses the initial report scan. For affected records, it searches each
current workspace parent once: RUNFORGE_WORKSPACE_ROOT when configured, otherwise
`runforge-workspaces` beside the recorded Git or snapshot source.
It does not search every disk or a previous container's workspace location.
Use the explicit checks below for workspaces and experiments outside the selected
paths. Legacy workspace formats are not listed. Verified-directory execution
creates no workspace.

```bash
runforge discover "$REPORT_ROOT"
runforge discover "$REPORT_ROOT" --older-than 6h
runforge workspaces "$WORKSPACE_ROOT"
```

The first scan includes recently interrupted work too. The second adds age
and local process hints to old inprogress records; it does not filter the list
or establish that they expired. An interruption can happen before a workspace
exists or after one is removed, so inspect reports even when no workspaces
remain. A workspace may also belong to a currently running worker.

Use the state and ownership evidence to select the next action:

| Recorded situation | Action |
| --- | --- |
| created or init, without a claim | Execute with run. |
| inprogress, or any remaining claim | Identify the old owner and confirm both worker and child stopped before recovery. Leave live or uncertain work alone. |
| failed, without a claim | Retry normally when another attempt is wanted. |
| completed | Keep the result; run and retry skip it. |
| held | Release the scheduling hold before attempting execution or retry. Release does not clear a claim. |
| Invalid metadata | Read the diagnostic and preserve the record/history while investigating; force does not repair invalid metadata. |

Ordinary run continues unrelated runnable plans and skips inprogress or claimed
work. A successful exit can therefore leave interrupted experiments unfinished.
Inspect the summary and discover again after recovery.

### Connect a leftover workspace to its experiment

Workspace listing prints its recorded host, PID, owner, and source. For an exact
match, read the workspace manifest and the candidate experiment's claim:

```bash
python3 -m json.tool "$WORKSPACE/runforge-worker.json"
python3 -m json.tool "$EXPERIMENT/claim/owner.json"
```

Match the manifest's worker_id to the claim's token. Owner or PID alone is not
unique: one runner can process several experiments, and PIDs can be reused.
The startup report performs this join for affected experiments under its selected
paths and current workspace parents. These manual checks work when investigating
other workspace locations. A missing or unreadable record means the association
is unknown; source identity alone is insufficient to establish which experiment
owns a workspace.

For Docker or a job scheduler, use its container/job history to establish that
the old execution stopped. A PID absent from the current container does not
establish that a process on another host or PID namespace stopped. For future
runs, RUNFORGE_CLAIM_OWNER can record a container execution or scheduler job ID
that you can trace externally.

### Restart confirmed interrupted experiments

Once both the former worker and child are confirmed stopped, name each affected
experiment explicitly:

```bash
runforge retry --force --stream-output "$EXPERIMENT"
runforge retry --force "$EXPERIMENT_A" "$EXPERIMENT_B"
```

This recovers failed or inprogress experiments, clears any previous claim, and
archives their previous attempt before starting a new one. Forced root-wide
recovery is refused; runforge retry --force must receive experiment directories.
For an interrupted created/init record that still has a claim, forced retry is
not supported: it skips that state without clearing the claim. Preserve the
record and investigate that claim separately; do not label it failed just to
make retry accept it.

Retry starts the recorded command as a new attempt. Git and snapshot sources use
a fresh workspace; verified-directory sources execute in the original directory.
It does not resume the old process. Ordinary retry moves saved outputs into
attempt-NNNN and starts with empty artifacts. To make previous artifacts available
at their usual paths for an application that already supports checkpoint recovery,
use [retry --reuse-artifacts](#reuse-artifacts-for-application-checkpoint-recovery).
It can be combined with --force after the same independent ownership check.

After preserving anything needed from a confirmed abandoned workspace, remove
that exact workspace and inspect the durable report again:

```bash
runforge workspaces "$WORKSPACE_ROOT" --remove "$WORKSPACE"
runforge discover "$REPORT_ROOT"
runforge run "$REPORT_ROOT"
```

Workspace removal is optional and separate from retry. It does not clear an
experiment's claim or change its status. Keep live and unidentified workspaces.

## Inspect published work

```bash
runforge discover "$REPORT_ROOT"
runforge discover "$REPORT_ROOT" --older-than 6h
runforge discover "$REPORT_ROOT" --started-before 2026-08-01T00:00:00Z
```

Discovery is read-only. It reports lifecycle, attempt, source, scheduling hold,
and path. Experiments are leaves: captured inputs, artifacts, source snapshots,
and retry archives beneath them are never discovered as additional experiments.
Hidden publication staging directories and directory symlinks are excluded.
A generic config.json alone does not hide plans below its directory. Damaged
RunForge metadata is reported as invalid; it is never executed.

Staleness flags are mutually exclusive hints based on start time, not automatic
recovery. Local host/PID evidence cannot establish liveness across machines or
PID namespaces. On Linux, a PID reused by a process started after the claim is
reported as dead. The hint does not change a claim or status.

## Execute available work

```bash
runforge run "$REPORT_ROOT"
runforge run "$REPORT_ROOT" --max-tasks 2 --stream-output
```

Selection consumes a path-ordered queue. When that queue is exhausted, the same
process discovers again and consumes newly published eligible plans. No scan runs
while an experiment is executing. It exits when a refill has no unvisited
eligible work, or its successful-completion quota is reached. It does not poll,
wait for future publications after becoming idle, or run as a daemon.

Only report-root selections refill. Explicit experiment leaves, including a
new plan executed by launch, finish without another discovery scan. Mixed
selections refill the report roots while retaining invocation-wide task counts.

Claims decide ownership across multiple processes. Each task is claimed
immediately before processing; status and scheduling metadata are reread under
that ownership. Claimed plans are skipped without consuming a completion slot.
Plans already attempted or contended during the invocation are not repeatedly
requeued. Failed experiments require explicit retry and are never automatically
reissued on refill.

--max-tasks bounds successful completions across all refills, not attempts.
Failures and contention do not consume the completion quota. A worker can
therefore attempt more than N experiments before completing N.

## Hold and release pending work

```bash
runforge schedule --hold "$EXPERIMENT"
runforge schedule --release "$EXPERIMENT"
```

Holds are separate scheduling metadata in schedule.json; execution status,
immutable configuration, attempts, logs, and artifacts remain unchanged. An
absent schedule file means not held. Invalid scheduling metadata prevents
selection and appears as invalid metadata. Priority controls are deferred.

A successful hold prevents future execution. Hold/release acquire the same claim
as execution, so an active claim is rejected. Release removes a scheduling hold;
it never removes an execution claim. Running tasks finish normally.

A plan held after queue creation is skipped at its execution boundary. A held
plan released while other tasks run can become eligible on the next refill.
An all-held or all-contended queue exits without a rescan busy loop.

F schedule accepts multiple explicit experiment/report-root paths. It preflights
discovery, deduplicates targets, and reports each successful edit. Busy-target
errors return 2; edits already successfully applied to other targets remain.

Hold/release can replace a malformed schedule.json under the same claim guard.
They still require valid config.json and status.json. Bulk edits continue on
valid experiments and report damaged metadata or busy targets with exit 2;
one bad record does not block every valid experiment. Ordinary discovery and
execution continue to reject malformed scheduling controls until repaired.

## One specified plan

```bash
runforge run "$EXPERIMENT"
```

run accepts experiment or report-root paths, unions overlapping selections, and treats each experiment as a leaf. Completed, held, failed, or already-claimed plans are reported as skips. Exit 0 does not assert that every target completed.

## Read outcomes

The batch summary counts unique discovered candidates, selected attempts,
successful completions, failures, non-runnable plans, claim contention,
stale-after-claim skips, held plans, quota-deferred candidates, and invalid
metadata. Held work is distinct from quota deferral. Counts reflect observed
selection state; reaching the quota does not force a final discovery pass.

Batch exit codes are 0 for no selected failure (including only skips), 1 for a
selected failure, and 2 for invalid metadata or CLI/domain errors. Invalid
metadata takes precedence over command failure. Individual child codes are
stored in status.json. An execution failure does not discard durable results
from other experiments.

If a discovery root disappears or becomes unreadable during queue refill, the
worker warns, stops refilling, and prints its accumulated summary with exit 2.
Already completed work remains completed and is not reissued.

## Interrupted runs and explicit retry

A killed process can leave a workspace, inprogress status, and a claim behind.
RunForge cannot resume the process or workspace, and does not infer abandonment
from age. First independently establish that the worker and child processes
have stopped, using the recorded host/owner and external process or job evidence.
RUNFORGE_CLAIM_OWNER can identify a scheduler job when host:PID is insufficient.

```bash
runforge retry "$EXPERIMENT"
runforge retry --force "$EXPERIMENT"
```

Retry archives previous status as status.snapshot.json and moves logs/artifacts
into attempt-NNNN before making another attempt. Archive errors abort without
silently losing history. Retry preparation errors count as invalid work (exit 2),
with no selected or failed execution added. The same immutable configuration is executed anew;
checkpoints are application-specific, not automatically selected by RunForge.
A held experiment must be released before retrying.

Force is an operator-confirmed escape hatch for interrupted or claimed work;
it cannot prove that a former process stopped. Never force a live experiment.

F retry can select failed plans under multiple roots. Force is refused unless
every explicitly supplied path is itself an experiment directory. Completed and
other ineligible states are skipped; failed attempts are retried once per
invocation, not repeatedly until they succeed.

A claimed failed experiment named explicitly is skipped with its recorded owner
and force-recovery guidance. The skip still returns 0; it does not prove a retry
ran. Confirm that the old worker and child stopped before using --force.

## Reuse artifacts for application checkpoint recovery

Use ordinary retry for fresh artifacts, or opt in to copying the immediately
preceding attempt's full artifact tree to the unchanged current artifact path:

```bash
runforge retry "$EXPERIMENT"
runforge retry --reuse-artifacts "$EXPERIMENT"
runforge retry --reuse-artifacts "$REPORT_ROOT"
```

The root form retries each eligible failed experiment using its own previous
artifacts. Only run the example that matches your intended retry. Reuse remains
subject to holds, claims, and eligible states. For a confirmed interrupted worker,
both flags are needed to override ownership and reuse its outputs:

```bash
# Only after confirming that the old worker AND child stopped:
runforge retry --force --reuse-artifacts "$EXPERIMENT"
```

The immutable recorded command and configuration are unchanged. The application
must already enable checkpoint loading from RUNFORGE_ARTIFACT_DIR (or its
recorded artifact-path argument). RunForge does not add resume flags, select a
checkpoint format, verify restored progress, or reuse the old worker workspace.
An empty artifact directory is valid to copy; it does not establish that a
checkpoint exists or that any progress was resumed.

Previous status, stdout/stderr logs, and original artifacts stay in attempt-NNNN.
The new attempt gets separate current logs and independent copied files: updating
them cannot modify the archive. Copying needs roughly one extra artifact tree's
worth of disk space; elapsed time depends on bytes, file count, and storage
throughput. No performance measurements are promised. The whole tree is copied,
including files unrelated to checkpoints. Regular files and directories are
supported; symbolic links and special files are rejected. Missing artifacts are
a preparation error. Ordinary retry keeps its existing fresh-output behavior.

Preparation checks and copies the tree in a hidden .attempt-NNNN.tmp-* directory
before moving old outputs. It publishes only a complete seed, then resets status
to init. Caught copy/status-reset errors roll back without starting a command or
incrementing the attempt; the batch reports invalid work with exit 2. If rollback
also fails, the error names the locations to preserve and inspect.

An abrupt termination can bypass rollback. A leftover staging directory for the
same attempt or an already-existing attempt archive blocks another retry with a
diagnostic. First confirm all prior workers and children stopped and preserve a
backup of the experiment and reported staging/archive paths. Inspect
status.snapshot.json, logs, and the original artifacts there before restoring a
consistent prior layout. The hidden .artifacts-seed is a working copy and may be
partial; preserve the original artifact tree as the recovery source. Do not
delete archives to bypass a collision or mark a partial seed runnable by editing
status. There is no automatic interrupted-preparation repair or guarantee of
power-loss atomicity.

## Temporary workspace identity and cleanup

```bash
runforge workspaces "$WORKSPACE_ROOT"
runforge workspaces "$WORKSPACE_ROOT" --remove "$WORKSPACE"
```

Listing is read-only and shows recorded source identity/reference, host, PID,
owner, and start time, or explicit unknown identity. These records do not prove
that a process is alive or abandoned. Confirm independently before removal.
When the manifest is missing or unreadable, a matching directory name alone
does not establish that the directory is a RunForge workspace. Inspect it before
removal.
Only explicitly named paths found directly under the workspace root can be
removed. Git worktrees are deregistered rather than merely deleted.

Set RUNFORGE_WORKSPACE_ROOT to an existing writable workspace parent for one location to inspect
and clean. It is a temporary workspace root, not a report root. Experiment
report directories, saved logs/artifacts, and immutable source records remain.
