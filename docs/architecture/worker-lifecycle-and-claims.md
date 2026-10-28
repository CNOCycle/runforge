# Worker lifecycle, lazy refill, and claims

The command runforge run selects work from report roots while the shared
single-experiment executor prepares sources, executes, and writes results.
It is a finite lazy scheduler: discovery occurs on startup and when a candidate
queue is exhausted, rather than on a periodic timer.

```text
discover published experiment leaves
  -> path-ordered eligible queue
  -> claim one task
  -> reread status and held metadata
  -> verify inputs and recorded source
  -> execute and save result with ownership fencing
  -> release claim
queue exhausted -> discover again
no unvisited eligible work, or completion quota reached -> summary and exit
```

Multiple workers may select identical paths. Exclusive claim-directory creation
and random ownership tokens establish ownership; order/start-time gaps do not.
Post-claim rereads prevent a stale snapshot from rerunning a peer's completed
plan. Every status write verifies the token. Claim cleanup errors do not turn
an already recorded successful task into a failure.

## Experiment boundaries

An experiment is a discovery leaf. Discovery stops before its inputs, artifacts,
source copy, and attempt archives. A metadata file plus the generated cmd.sh,
or a parseable RunForge metadata kind identifies a boundary.
A lone ordinary application config does not define a plan. Recognized incomplete
or malformed experiment metadata is diagnostic, never executable. A standalone
config.json or status.json that cannot be parsed is reported. Without independent
RunForge evidence, discovery continues below that directory so unrelated files
cannot hide real experiments. Directory
symlinks and hidden publication staging trees are not followed.

## Holds and ownership

schedule.json stores a versioned scheduling hold independently of status.json
and config.json. Hold/release acquire execution's claim before validation and an
atomic metadata update. An acquired execution claim wins over a concurrent edit;
a successful hold is reread by future execution before any attempt begins.
No timeout, lease, heartbeat, priority, or process preemption is introduced.

## Refill accounting

Completion quotas and attempted/contended path sets belong to the invocation,
not each snapshot. Failed tasks are not automatically retried; contended paths
are not repeatedly scanned in a busy loop. Held paths can become eligible after
release at a later refill while other tasks remain. If no work is available,
the process exits; future publication requires another invocation.

## Failure and recovery

An abrupt kill can bypass Python cleanup, leaving a workspace, claim, and
inprogress status. There is no process reattachment, checkpoint selection,
liveness lease, or automatic recovery. Explicit forced retry requires independent
operator confirmation; it is not a guarantee against a still-live prior child.
Previous attempt history is archived before another attempt begins. Normal retry
preparation and execution share claim ownership.

Workspace identity is diagnostic only. Listing/inspection is not an abandonment
check. Cleanup removes only explicitly named discovered workspaces, including
Git worktree deregistration. The workspace root (RUNFORGE_WORKSPACE_ROOT) is separate from the
persistent experiment report root.
