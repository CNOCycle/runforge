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
takes uses it, including from `runforge worker`:

```bash
# Slurm batch script
export RUNFORGE_CLAIM_OWNER="slurm:${SLURM_JOB_ID}"
runforge worker "$REPORT_ROOT"
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
3. Confirm the job is gone — `squeue -j JOBID`, or `sacct -j JOBID` for a job
   that has already left the queue.
4. `runforge retry --force EXPERIMENT_DIR`.

Step 3 is the one that matters. `--force` is an escape hatch, not a concurrency
guarantee: using it while the original process still runs can execute the
experiment twice, and RunForge cannot detect that.

## Preemption And Time Limits

A worker killed by a scheduler time limit or preemption leaves its experiment
`inprogress` with a claim. There is no heartbeat, so nothing notices
automatically. Budget a `discover` pass after a batch finishes rather than
assuming a clean exit.

Sizing `--max-tasks` below what fits in the job's remaining walltime reduces how
much work is interrupted, though it cannot eliminate it for a long single
experiment.
