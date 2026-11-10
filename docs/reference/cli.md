# CLI reference

RunForge publishes immutable experiment plans and executes them under atomic
claims. Git and directory-snapshot commands run in a temporary workdir inside
the experiment; verified-directory commands run in their original directory.
Priority controls are deferred.

## Canonical operations

| Command after runforge | Effect | Outcome |
| --- | --- | --- |
| plan [--matrix FILE] | Publish one plan or a matrix; never execute. | 0, or 2 on error |
| launch | Publish and execute exactly one new plan. | Aggregate 0/1/2 |
| run PATH... | Execute eligible experiment leaves beneath explicit paths or patterns. | Aggregate 0/1/2 |
| retry PATH... | Archive and retry failed selected plans. | Aggregate 0/1/2 |
| onstart --single-node PATH | Entry point for an interruptible machine: restart interrupted experiments, then run the rest. | Aggregate 0/1/2 |
| discover [PATH...] | Union/deduplicate read-only experiment discovery. | 0, or 2 on invalid metadata |
| mapping ARTIFACT | Read a saved matrix mapping. | 0, or 2 on error |
| schedule (--hold \| --release) PATH... | Change scheduling holds, not claims or history. | 0, or 2 on rejected targets |

Global -h/--help and -v/--version require no experiment execution. Family help
and leaf help describe their own required arguments and semantic defaults.

## Common workflows

```bash
runforge plan --source-path "$SOURCE" --out-dir "$REPORT_ROOT" -- python train.py
runforge launch --source-path "$SOURCE" --out-dir "$REPORT_ROOT" --stream-output -- python train.py
runforge plan --matrix "$AXES" --source-path "$SOURCE" --out-dir "$REPORT_ROOT" -- python train.py --seed='{SEED}'
runforge mapping "$MAPPING_ARTIFACT"
runforge discover "$REPORT_ROOT"
runforge run "$REPORT_ROOT" --max-tasks 2 --stream-output
runforge run "$REPORT_ROOT/**/*baseline*"
runforge schedule --hold "$EXPERIMENT"
runforge schedule --release "$EXPERIMENT"
runforge schedule --hold "$EXPERIMENT1" "$EXPERIMENT2"
runforge schedule --hold "$REPORT_ROOT/**/*baseline*"
runforge retry "$EXPERIMENT"
runforge retry "$REPORT_ROOT/**/*baseline*"
runforge onstart --single-node "$REPORT_ROOT"
runforge --version
```

run accepts experiment or report-root paths, unions overlapping selections, and treats each experiment as a leaf. Completed, held, failed, or already-claimed plans are reported as skips. Exit 0 does not assert that every target completed.

## Planning options and source modes

plan, launch, and matrix planning share --name (exp), --source-path (current
directory), --source-mode (current-head), --out-dir, --commit, --patch,
--env-file, --input-tree, --shell, and the child command after --.
Source modes are current-head, pinned-git, verified-directory, and
directory-snapshot. Pinned Git requires --commit and alone permits --patch.
Git output defaults to SOURCE_REPOSITORY/reports; non-Git modes require an
explicit report root outside the source. Configuration capture and source
validation rules are defined in [source modes](source-modes.md) and
[planned inputs](../guides/planned-configuration-inputs.md).

Everything after -- belongs to the child, including words matching RunForge
command names. It is an argv array unless --shell explicitly selects one quoted
script. Place planning options before --. No implicit shell execution is added.
Only launch, run, and retry expose -s/--stream-output, preserving
stdout.log and stderr.log as well as optional console mirroring. run and retry
also accept report-root paths, which select the experiments beneath them.

## Path patterns

`discover`, `run`, `retry` (with or without `--force`) and `schedule` accept quoted
wildcard patterns wherever they accept an experiment or report-root path, for example
`runforge run "$REPORT_ROOT/**/*baseline*"`. RunForge expands the pattern itself;
one rule applies to every command.

- Syntax: `*`, `?`, `[...]` within one path component, and at most one `**`, which
  matches any number of directory levels, including none. More than one `**` is an
  error. Patterns match paths, not recorded experiment names, and are not regular
  expressions. Only directories match, so matrix mapping files are ignored.
- Matching follows `runforge discover`: it never enters hidden directories or
  symbolic links and stops at an experiment, even a damaged one.
- An existing path is used as written, even with `[` or `*` in its name, and so are
  the leading directories of a pattern that exist. After the first wildcard,
  `*`, `?` and `[` in a name are pattern syntax; there is no escape.
- A pattern that matches no directory, or a directory that cannot be read, exits 2
  before the command acts. `run` and `retry` still require an experiment under every
  matched directory; `schedule` skips matched directories without experiments.
- Quote the pattern. An unquoted pattern is expanded by the shell, which also passes
  matrix mapping files as paths; unquoted `**` recurses only after
  `shopt -s globstar` in Bash.
- `retry --force` keeps its rule that every path must be an experiment directory, so a
  pattern that also matches a report root is refused as a whole. Confirm that old
  workers and children stopped, and preview the matches with `runforge discover`.
- `run` expands a pattern once at start; the refill rediscovery covers the matched
  directories, so an experiment published later is found only under a matched report
  root.

## Holds, quotas, and errors

schedule holds are separate from lifecycle status and immutable configuration.
Active ownership rejects an edit. Invalid control metadata prevents selection;
priority is neither accepted nor advertised. See [worker behavior and recovery](../guides/workers-and-recovery.md) for ownership, refill, and retry details.

Schedule accepts multiple experiment paths, report roots, or
[path patterns](#path-patterns), for both `--hold` and `--release`. It skips matched
directories that contain no experiments, and exits 2 before editing holds if no
experiments are found at all. Review the selection with `runforge discover` first.

-n/--max-tasks is a positive successful-completion quota across queue refills.
The batch process discovers again only after exhausting its queue; it exits if
no unvisited eligible work is available. It does not poll for future publication.
Failed attempts, stale candidates, holds, and contention do not consume the quota.

Aggregate exit 0 means no selected failure, including only skipped work; 1 means
an attempted command failed; 2 means invalid metadata or a CLI/domain error.
Invalid metadata takes precedence over failure. Detailed child codes are stored
in status.json. Completed work is not rerun and retries are explicit.

launch/run/retry all use the aggregate contract. run/retry require explicit
paths or patterns; an empty or missing input path is an error before any execution.
Paths are unioned and deduplicated. Force retry requires every path, after any
pattern is expanded, to be an experiment directory; bulk forced root recovery is
refused.

--older-than and --started-before are mutually exclusive discovery hints based
on experiment start time. They do not recover tasks or remove claims. -f/--force
is only an operator-confirmed retry override after independently checking that
prior worker/child processes stopped; it cannot prove abandonment.

retry --reuse-artifacts copies the immediately preceding attempt's entire
artifact tree into the current artifact directory after archiving the original.
It is disabled by default; normal retry starts with empty artifacts. The recorded
command must already support checkpoint recovery. Copies are independent of the
archive, current logs are fresh, and symbolic links/special files are rejected.
Root selection reuses each eligible failed experiment's own artifacts. Combining
it with --force still requires experiment directories (explicit or from a pattern)
and confirmation that prior workers and children stopped. See the [checkpoint recovery guide](../guides/workers-and-recovery.md#reuse-artifacts-for-application-checkpoint-recovery)
for costs, limitations, and interrupted preparation.

## onstart

```bash
runforge onstart --single-node "$REPORT_ROOT"
```

The entry point for an interruptible machine: one that can be stopped at any
time and started again later. Run it once each time the machine starts, after
the plans are published, for example
`[ -f "$READY_FLAG" ] && runforge onstart --single-node "$REPORT_ROOT"`. It is
not a daemon; it returns when the work is done.

It does two things, in order:

1. Restarts every experiment under the report root that an interruption left
   `inprogress`, as `retry --force --reuse-artifacts` would.
2. Processes the remaining plans exactly as `run` does.

**Warning: your program must be resumable.** RunForge restarts the recorded
command as a new attempt and copies the previous attempt's artifacts back to
`RUNFORGE_ARTIFACT_DIR`. It does not resume the process: a program that does not
load its own checkpoint from that directory starts over.

**Warning: one machine only.** The restarts are forced. A forced retry of an
experiment that is still running starts it a second time, and RunForge cannot
detect that. Use `onstart` only on a single machine that has just started, while
no other machine works on the report root. Every invocation prints this warning,
and without `--single-node` the command stops with exit 2 having changed nothing.
On a cluster or a shared report root, use `run` and a manual `retry`
([Workers and recovery](../guides/workers-and-recovery.md)).

An interruption kills the worker and the program but leaves the experiment
`inprogress` with its claim: the status is stale. `onstart` clears the claim,
archives the interrupted attempt under `attempt-NNNN`, and starts a new attempt
that continues from the restored artifacts. The step-by-step description is in
[Workers and recovery](../guides/workers-and-recovery.md#what-happens-to-an-interrupted-task).

Behavior to know:

- Use exactly one `onstart` entry point per machine, on the report root. While
  it runs, `.onstart.lock` in the report root is locked, so a second `onstart`
  on the same path and host exits 2. The lock does not cover other hosts,
  overlapping paths (a report root and one of its subdirectories), or a
  training process that outlived a killed RunForge process.
- An experiment that cannot be restarted, for example because its artifact
  tree contains a symbolic link, is reported and the command exits 2. `onstart`
  makes no second attempt and does not retry it without its artifacts. It does
  not promise that nothing changed: the status stays `inprogress` and is not
  rewritten, but the forced retry has already cleared the old claim, and a
  failed rollback can leave files that the message asks you to inspect. Read
  the message before touching the experiment.
- Holds take precedence. An `inprogress` experiment that is held is not
  restarted and not released; it is listed before the restarts and again at the
  end. RunForge cannot change scheduling for an `inprogress` experiment, so
  remove its `schedule.json` (an absent file means not held) and run `onstart`
  again to restart it. Held plans are skips, so they do not change the exit
  code, as with `run`. This state only arises through manual editing of
  `schedule.json`.
- Experiments in `created` or `init` that still hold a claim are left unchanged
  and listed in the same way, because a forced retry does not clear them. Failed
  experiments are not retried.
- The exit code is 0 when every step exited 0, otherwise the largest exit code
  among the restarts and the final run.

## Environment and output

| Variable | Role |
| --- | --- |
| RUNFORGE_CLAIM_OWNER | Diagnostic claim owner, such as host:PID or scheduler job ID. |
| RUNFORGE_ARTIFACT_DIR | Child output-artifact directory, supplied by RunForge. |
| RUNFORGE_INPUT_DIR | Child immutable input tree, supplied by RunForge. |

Git and directory-snapshot executions run in `.workspace/` inside the
experiment, removed after the command exits. A leftover one is deleted and
rebuilt by the next execution of that experiment, never reused.
Effective arguments print paths/choices and environment key names, never ambient
or captured environment values. Each selected task reports its environment keys,
artifact directory, and executing attempt number before the child command begins.
Run arguments report the completion quota; retry arguments report force and
artifact reuse.
Progress, logs, summaries, matrix tables, and
warnings remain human-readable; no machine-readable output protocol is added.

Before execution, run/retry/launch assess inprogress or claimed experiments from
their initial selection as active, stale, or unknown. Active claims (a running
local PID that started before the claim) are not reported. Stale and unknown
records are reported with recorded ownership, process evidence, any workdir
present in the experiment, and manual recovery commands. Inspection is read-only
and does not change selection, exit accounting, or claims. Unrelated runnable
work continues. The check reuses discovery for affected records. See the
[pre-start recovery workflow](../guides/workers-and-recovery.md#before-starting-again-after-a-restart).
