# Slurm And Containers

Recovering an interrupted run means answering one question: is the process that
holds this claim still alive? The default recorded identity answers it on a
native host and not much else. This page is about fixing that.

## The Default And Its Limits

A claim records `HOSTNAME:PID` by default:

```text
error: Experiment is already claimed: /reports/main/01234567_baseline_0000 (held by node7:48213 since 2026-07-30T09:15:02Z)
```

On a native Linux host that is directly actionable: connect to `node7` and run
`ps -p 48213`.

Two environments break it. Inside a **container**, the hostname is an
image-local identifier and process ids are namespaced, so the pair usually
cannot be resolved from outside — and the container may be gone entirely. Under
a **batch scheduler**, a job id is far more useful than a pid, because
`squeue -j` answers the liveness question directly.

## Recording A Resolvable Identity

Set `RUNFORGE_CLAIM_OWNER` in the worker's environment. Every claim that process
takes uses it, including from `runforge run`:

```bash
# Slurm batch script
export RUNFORGE_CLAIM_OWNER="slurm:${SLURM_JOB_ID}"
runforge run "$REPORT_ROOT"
```

```bash
# container started by an orchestrator
docker run -e RUNFORGE_CLAIM_OWNER="pod/${POD_NAME}" ...
```

The owner resolves from an explicit API argument, then the variable, then
`HOSTNAME:PID`. A blank or whitespace-only value falls back to the default
rather than recording an empty owner.

This value is printed in operator error messages, so it must not contain
secrets.

## Recovery Under A Scheduler

1. `runforge discover "$REPORT_ROOT"` to find experiments stuck `inprogress`.
2. Read the owner from the error message, or from `claim/owner.json`.
3. Check the job with `squeue -j JOBID` or `sacct -j JOBID` after it leaves
   the queue. Independently confirm that both the worker and any child process
   it started have stopped; a job record alone does not establish that every
   child has stopped.
4. For a failed or `inprogress` experiment, run
   `runforge retry --force EXPERIMENT_DIR`. Forced retry does not clear a claim
   on a `created` or `init` experiment; preserve that record and investigate.

Step 3, confirming both worker and child have stopped, is the one that matters. `--force` is an escape hatch, not a concurrency
guarantee: using it while the original process still runs can execute the
experiment twice, and RunForge cannot detect that.

## Preemption And Time Limits

A worker killed by a scheduler time limit or preemption can leave its experiment
`inprogress` with a claim. There is no heartbeat, so nothing notices
automatically. Budget a `discover` pass after a batch finishes rather than
assuming a clean exit.

Sizing `--max-tasks` below what fits in the job's remaining walltime reduces how
much work is interrupted, though it cannot eliminate it for a long single
experiment.
