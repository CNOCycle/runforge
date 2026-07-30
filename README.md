# RunForge

**RunForge** is a small experiment runner for turning a source checkout, command,
environment overrides, and output directory into a durable run record. It is
designed for the common research problem where an experiment is planned on one
machine, executed later on another worker, and still needs to reflect the exact
source state the planner intended.

The first-class workflow is Git-backed. A plan can either capture the current
`HEAD` plus tracked changes or use an explicit pinned commit/ref and optional
external patch. Both source modes can expand a deterministic parameter matrix
into independent plans. Planning never executes the experiment command.
Running later reconstructs the normalized source in a detached worktree.
`verified-directory` and `directory-snapshot` extend the same planner and
worker to simple-script directories that are not Git repositories, or that a
user simply prefers to plan directly; see
[Non-Git Directory Sources](#non-git-directory-sources).

RunForge is organized around two roles. A **planner** creates immutable
experiment directories containing source identity, rendered commands,
parameters, environment overrides, initial status, and reproduction helpers. A
**worker** reconstructs one explicit plan, runs it, and updates status, logs, and
artifacts.

## Project Structure

The Python package is grouped by responsibility:

```text
src/runforge/
  cli/             # argparse tree, request translation, output, and dispatch
  schemas/         # versioned experiment and source data contracts
  planning/        # source resolution, matrices, and plan publication
  execution/       # discovery, retry, and worker execution
  infrastructure/  # experiment storage, Git, atomic JSON, and clock access
```

The test tree mirrors these packages. Standard experiment filenames and typed
configuration/status persistence are centralized in `ExperimentDirectory`, so
planning, execution, discovery, retry, and CLI summaries share one on-disk
storage definition.

## Install

From this project root:

```bash
python -m pip install .
```

For local development:

```bash
python -m pip install -e '.[dev]'
```

The console command is `runforge`. `python -m runforge` is an
equivalent entry point.

## Quick Start

Choose the source repository and a persistent report root explicitly. If
`--out-dir` is omitted, RunForge uses `$REPO/reports` by default:

```bash
REPO=/path/to/your-project
REPORT_ROOT="$REPO/reports"

runforge plan \
  --name baseline \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  -- python train.py --output '{ARTIFACT_DIR}'
```

You may run `runforge plan` from any shell working directory when
`--source-path` is an absolute path. The command after `--` is interpreted from
the root of the recorded source repository, and the worker later executes it
from a detached worktree of that repository. In the example above,
`python train.py` means `$REPO/train.py`.

Planning confirms where the experiment plan was created:

```text
Experiment plan created at: $REPO/reports/main/01234567_baseline_0000
```

The directory name follows this rule:

```text
REPORT_ROOT/{BRANCH_SLUG}/{COMMIT8}_{NAME_SLUG}_{COUNT}
```

For example:

```text
$REPO/reports/main/01234567_baseline_0000
```

`COMMIT8` is the first 8 hexadecimal characters of the recorded commit.
`NAME_SLUG` comes from `--name`, with unsafe path characters replaced by `-`.
`COUNT` starts at `0` and increments when a matching experiment directory already
exists.

Run that exact directory on the same or another machine with access to the
recorded repository:

```bash
runforge run "$REPO/reports/main/01234567_baseline_0000"
```

The worker returns the planned command's exit code. A non-zero command exit
marks the experiment as failed but still leaves logs and metadata intact.

To create the plan and run it immediately in the foreground, use `launch` with
the same options accepted by `plan`:

```bash
runforge launch \
  --name baseline \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  -- python train.py --output '{ARTIFACT_DIR}'
```

`launch` prints the created experiment directory before execution and returns
the training command's exit code. The standalone `plan` and `run` commands
remain available when planning and execution need to happen at different times
or on different machines.

By default, command output is written only to `stdout.log` and `stderr.log`.
Add `--stream-output` to `launch` or `run` to also view both streams in the
console while preserving those log files:

```bash
runforge launch --stream-output \
  --name baseline \
  --source-path "$REPO" \
  -- python train.py --output '{ARTIFACT_DIR}'

runforge run --stream-output "$REPO/reports/main/01234567_baseline_0000"
```

RunForge forwards output as the command emits it. Programs that buffer their
own output must flush it or enable their own unbuffered mode for timely display.
Regardless of streaming mode, `run`, `launch`, `retry`, and `worker` print
flushed preparation, execution, and final success or failure messages so
a quiet child process is distinguishable from a stalled CLI. In log-only mode,
the lifecycle messages also identify the active `stdout.log` and `stderr.log`
paths.

## Retry A Failed Or Interrupted Experiment

Start another attempt of the same immutable configuration, archiving the
previous one under `attempt-NNNN/`:

```bash
runforge retry "$REPORT_ROOT/main/01234567_baseline_0000"
```

An `inprogress` or claimed experiment requires `--force` after you have
confirmed the original process stopped. See
[Workers and recovery](docs/guides/workers-and-recovery.md).

## Pinned Git Source

Plan from an explicit commit or ref rather than the current checkout:

```bash
runforge plan --name release-baseline --out-dir "$REPORT_ROOT" --source-path "$REPO" --source-mode pinned-git --commit v1.2.0 -- python train.py --output '{ARTIFACT_DIR}'
```

See [Source modes](docs/reference/source-modes.md) for all four modes.

## Parameter Matrices

Expand one source and command template into an independent experiment directory
per parameter combination:

```bash
runforge matrix --name sweep --matrix-file matrix.json --out-dir "$REPORT_ROOT" --source-path "$REPO" -- python train.py --lr '{LR}' --output '{ARTIFACT_DIR}'
```

Planning prints a table relating each directory to its parameters and saves it
beside them as a JSON artifact, which `runforge matrix-show PATH` renders later.
See [Matrices and mapping](docs/guides/matrices-and-mapping.md).

## Non-Git Directory Sources

Two modes plan a plain directory rather than a Git repository.
`verified-directory` executes the original in place and re-verifies it before
every run; `directory-snapshot` captures a self-contained copy. Both require
`--out-dir` to resolve outside `--source-path`.

```bash
runforge plan --name ablation --source-mode directory-snapshot --source-path /work/study --out-dir /work/study-reports -- python train.py --output '{ARTIFACT_DIR}'
```

See [Non-Git directory sources](docs/guides/non-git-sources.md).

## Discover Planned Experiments

List what a report root contains, without changing anything:

```bash
runforge discover "$REPORT_ROOT"
```

See [Workers and recovery](docs/guides/workers-and-recovery.md).

## Run Multiple Experiments With A Worker

Consume one snapshot of runnable plans, claiming each atomically so several
workers can share a report root:

```bash
runforge worker "$REPORT_ROOT"
```

See [Workers and recovery](docs/guides/workers-and-recovery.md) for the summary
counts, exit codes, and recovery of interrupted runs.

## Where To Save Results

Use a project-specific report root so experiment directories are easy to trace
back to their source project. By default, RunForge writes to `$REPO/reports`.
Add `reports/` to that repository's `.gitignore` so planned experiments do not
appear as untracked source files.

A nested, sibling, or shared path is also fine when it includes the project name,
for example `$REPO/reports/runforge` or
`$REPO/../runforge-reports/your-project`. Avoid anonymous shared folders that
mix reports from unrelated projects. Pass `{ARTIFACT_DIR}` to your command, or
read `RUNFORGE_ARTIFACT_DIR`, so outputs land inside the planned experiment
artifact directory.

## Pipelines And `{ARTIFACT_DIR}`

Keep long or multi-step commands in a tracked wrapper script and write output
below `RUNFORGE_ARTIFACT_DIR`. See
[Long pipeline commands](docs/guides/long-pipelines.md) and
[Dynamic environment variables](docs/guides/dynamic-environment-variables.md).

## Planned Configuration Inputs

Some programs receive output paths and downstream checkpoint locations through
configuration files rather than command arguments. Use `--input-tree` to
capture a configuration directory as immutable per-experiment inputs. JSON
files render structurally; YAML, YML, and INI files render as text while
preserving their layout and comments, then receive syntax validation. All
templated formats support
`{ARTIFACT_DIR}`, `{INPUT_DIR}`, and matrix parameters.

```bash
runforge launch \
  --name train-evaluate \
  --source-path "$REPO" \
  --input-tree "$REPO/configs" \
  --shell -- \
  "python train.py '{INPUT_DIR}/train.json' && python evaluate.py '{INPUT_DIR}/eval.json'"
```

The detailed
[planned configuration inputs guide](docs/guides/planned-configuration-inputs.md)
shows how linked training and evaluation files share one artifact layout.

## Advanced: Environment Overrides

Most runs can skip this. Use an environment file when the experiment needs
explicit machine-specific variables:

```text
# runforge.env
RUN_MODE=ablation
CUDA_VISIBLE_DEVICES=0
```

```bash
runforge plan \
  --name gpu-baseline \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  --env-file runforge.env \
  -- python train.py --output '{ARTIFACT_DIR}'
```

Only values in `--env-file` are stored in `config.json`; the ambient process
environment is not serialized.

## Experiment Layout

```text
REPORT_ROOT/BRANCH_SLUG/COMMIT8_NAME_SLUG_COUNT/
  config.json   status.json   cmd.sh
  stdout.log    stderr.log    artifacts/
```

The lifecycle is `created -> init -> inprogress -> completed|failed`. See
[Experiment layout](docs/reference/experiment-layout.md) for every file, who
writes it, and the non-Git variants.

## Reproducibility Boundary

RunForge captures source identity, the rendered command, explicitly requested
environment overrides, and rendered configuration inputs. It does not capture
untracked file contents, the ambient environment, your data, or your Python
environment. See
[Reproducibility boundary](docs/reference/reproducibility-boundary.md).

## License

RunForge is licensed under the
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
