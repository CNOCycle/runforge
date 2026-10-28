# CLI reference: candidate F

RunForge publishes immutable experiment plans, executes them under atomic claims,
and keeps report history separate from temporary workspaces. This candidate is
an independent whole-interface alternative. Priority controls are deferred.

## Canonical operations

| Command after runforge | Effect | Outcome |
| --- | --- | --- |
| plan [--matrix FILE] | Publish one plan or a matrix; never execute. | 0, or 2 on error |
| launch | Publish and execute exactly one new plan. | Aggregate 0/1/2 |
| run PATH... | Execute eligible experiment leaves beneath explicit paths. | Aggregate 0/1/2 |
| retry PATH... | Archive and retry failed selected plans. | Aggregate 0/1/2 |
| discover [PATH...] | Union/deduplicate read-only experiment discovery. | 0, or 2 on invalid metadata |
| mapping ARTIFACT | Read a saved matrix mapping. | 0, or 2 on error |
| schedule (--hold \| --release) PATH... | Change scheduling holds, not claims or history. | 0, or 2 on rejected targets |
| workspaces [ROOT] [--remove PATH...] | List recorded identity; remove only exact named targets. | 0, or 2 on error |

Global -h/--help and -v/--version require no experiment execution. Family help
and leaf help describe their own required arguments and semantic defaults.
No legacy command adapters are supplied in these greenfield alternatives.

## Common workflows

```bash
runforge plan --source-path "$SOURCE" --out-dir "$REPORT_ROOT" -- python train.py
runforge launch --source-path "$SOURCE" --out-dir "$REPORT_ROOT" --stream-output -- python train.py
runforge plan --matrix "$AXES" --source-path "$SOURCE" --out-dir "$REPORT_ROOT" -- python train.py --seed='{SEED}'
runforge mapping "$MAPPING_ARTIFACT"
runforge discover "$REPORT_ROOT"
runforge run "$REPORT_ROOT" --max-tasks 2 --stream-output
runforge schedule --hold "$EXPERIMENT"
runforge schedule --release "$EXPERIMENT"
runforge retry "$EXPERIMENT"
runforge workspaces "$WORKSPACE_ROOT"
runforge workspaces "$WORKSPACE_ROOT" --remove "$WORKSPACE"
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
explicit report root outside the source. Configuration capture/source validation
rules are unchanged; see [source modes](source-modes.md) and
[planned inputs](../guides/planned-configuration-inputs.md).

Everything after -- belongs to the child, including words matching RunForge
command names. It is an argv array unless --shell explicitly selects one quoted
script. Place planning options before --. No implicit shell execution is added.
Only launch, run, retry, and root processing expose -s/--stream-output, preserving
stdout.log and stderr.log as well as optional console mirroring.

## Holds, quotas, and errors

schedule holds are separate from lifecycle status and immutable configuration.
Active ownership rejects an edit. Invalid control metadata prevents selection;
priority is neither accepted nor advertised. See [worker behavior and recovery](../guides/workers-and-recovery.md) for ownership, refill, and retry details.

-n/--max-tasks is a positive successful-completion quota across queue refills.
The batch process discovers again only after exhausting its queue; it exits if
no unvisited eligible work is available. It does not poll for future publication.
Failed attempts, stale candidates, holds, and contention do not consume the quota.

Aggregate exit 0 means no selected failure, including only skipped work; 1 means
an attempted command failed; 2 means invalid metadata or a CLI/domain error.
Invalid metadata takes precedence over failure. Detailed child codes are stored
in status.json. Completed work is not rerun and retries are explicit.

F launch/run/retry all use the aggregate contract. run/retry require explicit
paths; an empty or missing input path is an error before any execution. Paths are
unioned and deduplicated. Force retry requires every explicit argument to be an
experiment directory; bulk forced root recovery is refused.

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
it with --force still requires explicit experiment paths and confirmation that
prior workers and children stopped. See the [checkpoint recovery guide](../guides/workers-and-recovery.md#reuse-artifacts-for-application-checkpoint-recovery)
for costs, limitations, and interrupted preparation.

## Environment and output

| Variable | Role |
| --- | --- |
| RUNFORGE_WORKSPACE_ROOT | Existing writable parent for temporary workspaces; separate from report roots. |
| RUNFORGE_CLAIM_OWNER | Diagnostic claim owner, such as host:PID or scheduler job ID. |
| RUNFORGE_ARTIFACT_DIR | Child output-artifact directory, supplied by RunForge. |
| RUNFORGE_INPUT_DIR | Child immutable input tree, supplied by RunForge. |

Unset workspace configuration uses the existing source-specific defaults.
An unusable configured parent fails rather than silently changing location.
Effective arguments print paths/choices and environment key names, never ambient
or captured environment values. Each selected task reports its environment keys,
artifact directory, and executing attempt number before the child command begins.
Run arguments report the completion quota; retry arguments report force and
artifact reuse.
Progress, logs, summaries, matrix tables, and
warnings remain human-readable; no machine-readable output protocol is added.
Workspace identity describes its recorded creator, not current process activity.
Cleanup requires independently confirmed abandonment and exact named targets.

Before execution, run/retry/launch assess inprogress or claimed experiments from
their initial selection as active, stale, or unknown. Active claims (a running
local PID that started before the claim) are not reported. Stale and unknown
records are reported with recorded ownership, process evidence, token-matched
workspaces, and manual recovery commands. Inspection is read-only
and does not change selection, exit accounting, or claims. Unrelated runnable
work continues. The check reuses discovery and searches only current workspace
parents for affected records; it is not a global workspace inventory. See the
[pre-start recovery workflow](../guides/workers-and-recovery.md#before-starting-again-after-a-restart).
