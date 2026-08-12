# Workers And Recovery

A report root can hold plans from any source — a matrix expansion, individual
`plan`/`launch` invocations, or a mix of both under the same folder — and
`discover` doesn't care which produced a given one: it recursively reports
the status of everything it finds there. Once there are enough that you want
to consume them from more than one process, or one was interrupted (including
killed by the system, not just failing normally), you need a way to find it
regardless of origin.

This guide covers inspecting planned work and its status irrespective of
origin, executing it safely in parallel, and the one supported recovery path.

## Inspecting Without Executing

`discover` scans a report root recursively and reports what it finds:

```bash
runforge discover "$REPORT_ROOT"
```

It prints one experiment per line with state, attempt, name, source identity,
and path, then counts per lifecycle state. It is strictly read-only: it never
executes commands, changes status, creates claims, or waits for another process.

It exits `0` after a clean scan and `2` when the root cannot be scanned or any
candidate's metadata is invalid. An empty scan is a success with zero totals.

Discovery only reports fully published plans, so it is safe to run while another
process is planning into the same root.

## Executing Available Work

`worker` consumes one finite snapshot of runnable plans:

```bash
runforge worker "$REPORT_ROOT"
```

It scans `created` and `init` plans in deterministic path order, atomically
claims each one immediately before executing it, and delegates to the same
executor `run` uses. If another worker already owns a plan, it skips that plan
and continues scanning rather than consuming a task quota slot.

Run several against one report root to parallelize. Only the atomic claim
decides ownership; ordering and start-time differences are not correctness
mechanisms.

The worker does not poll, rescan, wait, or run as a daemon. Work published after
its snapshot is handled by a later invocation — so on a root that is still being
planned into, run `worker` again when planning finishes.

Bound a temporary node with `--max-tasks`:

```bash
runforge worker "$REPORT_ROOT" --max-tasks 2
```

`N` must be positive. It means “complete `N` tasks,” not “inspect only `N`
candidates.” Claim contention, stale-after-claim skips, and failed executions
do not consume the completion quota; the worker continues scanning the initial
snapshot after each of them. It stops when the quota is reached or when the
snapshot is exhausted. Add `--stream-output` to mirror child output to the
console while still writing `stdout.log` and `stderr.log`.

## Reading The Summary

```text
Worker summary:
  candidates: 5
  selected: 2
  completed: 2
  failed: 0
  skipped: 1
    non-runnable: 1
    claim contention: 0
    stale after claim: 0
  deferred: 3
  invalid: 0
```

`selected` counts tasks claimed for processing by this invocation. `completed`
counts successful tasks and is the counter bounded by `--max-tasks`. `deferred`
counts runnable snapshot entries that were not scanned because the task quota
was reached. The three skip reasons answer different questions. **non-runnable**
counts plans
that were never eligible, including ones an earlier invocation already
completed. **claim contention** counts plans another worker owned. **stale after
claim** counts plans that were finished by a peer between this worker's scan and
its claim — a normal outcome when several workers share a root, not a failure.

Exit status is `2` when any candidate's metadata is invalid, `1` when a selected
command fails, and `0` otherwise. Note that `2` takes precedence: it means the
scan itself is untrustworthy, so a caller testing for `1` must not conclude that
every candidate was inspected.

## What The Worker Deliberately Does Not Do

There is no lease, heartbeat, timeout, or automatic stale-task recovery. If a
worker is killed while executing, its experiment stays `inprogress` with a claim
that no live process owns. RunForge does not guess whether a long-running
experiment is dead, because it cannot distinguish that from one that is merely
slow.

Recovery is therefore explicit and operator-confirmed.

## Finding A Possibly-Stale Run

The previous section describes what RunForge won't do automatically; this is
how you find a candidate for the manual recovery below, instead of already
having to know one exists. Add `--older-than` or `--updated-before` to
`discover` to flag `inprogress` experiments that started a while ago:

```bash
runforge discover "$REPORT_ROOT" --older-than 6h
runforge discover "$REPORT_ROOT" --updated-before 2026-08-01T00:00:00Z
```

Both are opt-in and purely a hint — nothing is retried, no claim is touched,
and passing neither leaves `discover`'s output exactly as it always was. A
flagged experiment gets one extra line naming how long it's been running and,
when the claim was acquired on this same host, whether that process is still
alive:

```text
inprogress | attempt=1 | name=baseline | source=main@a1b2c3d4 | path=/reports/main/01234567_baseline_0000
  stale: inprogress for 8h12m, started 2026-08-12T02:00:00.000000Z — process appears dead: PID 4127 not running on node7
```

"Process appears dead" only means the PID isn't currently running on *this*
host — not proof the original worker is gone, since a reused PID or a claim
made from a different host both read as "cannot verify liveness" instead.
Confirm independently before using `retry --force`, exactly as below.

## Recovering An Interrupted Run

First find out who held the claim. Commands that refuse to act on a claimed
experiment tell you:

```text
error: Experiment is already claimed: /reports/main/01234567_baseline_0000 (held by node7:48213 since 2026-07-30T09:15:02Z)
```

The owner defaults to `HOSTNAME:PID`, which you can check with `ps -p 48213` on
that host. In containers and under batch schedulers that pair is often not
resolvable from outside, so set `RUNFORGE_CLAIM_OWNER` to something you can
check, such as a Slurm job id.

After independently confirming the process has stopped:

```bash
runforge retry --force "$REPORT_ROOT/main/01234567_baseline_0000"
```

`--force` is a single-controller escape hatch, not a concurrency guarantee.
RunForge cannot prove another worker is inactive, so using it while the original
process still runs can execute the experiment twice.

## Retry Semantics

`retry` starts another attempt of the same immutable configuration. It does not
resume a child process or select a checkpoint.

- `failed` retries normally.
- `inprogress` requires `--force`.
- A claimed experiment requires `--force` in any state, because a claim may mean
  a live worker.
- `completed` is rejected; successful history is not overwritten. Plan a new
  experiment instead.
- `created` and `init` are rejected because `run` already handles them.

Before resetting anything, retry archives the previous status, logs, and
artifacts under `attempt-NNNN/`. Archive failure aborts without changing
runnable state, so a rejected retry leaves the experiment exactly as it was.

The warning that a forced retry cannot prove the previous worker stopped is
printed only when something was actually overridden — an `inprogress` state or a
claim that had to be cleared — so it stays meaningful.

## Running One Experiment Directly

When you want a specific plan rather than whatever is available:

```bash
runforge run "$REPORT_ROOT/main/01234567_baseline_0000"
```

`run` uses the same claim-aware boundary as `worker`, so it is safe to use
alongside workers on the same root. It returns the command's exit code.
