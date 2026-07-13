# RunForge

**RunForge** is a small experiment runner for turning a source checkout, command,
environment overrides, and output directory into a durable run record. It is
designed for the common research problem where an experiment is planned on one
machine, executed later on another worker, and still needs to reflect the exact
source state the planner intended.

The current first-class workflow is Git-backed. Planning records the current
`HEAD`, branch, tracked diff, rendered command, and explicit environment
overrides without executing anything. Running later recreates that source state
in a detached worktree, applies the recorded patch, executes the command, and
writes logs, status, and artifacts beside the metadata.

RunForge is organized around two roles. A **planner** creates one immutable
experiment directory containing the source identity, rendered command,
environment overrides, initial status, and reproduction helpers. A **worker**
takes one explicit experiment directory, reconstructs the recorded source state,
runs the command, and updates status, logs, and artifacts. 

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
Experiment plan created at: $REPO/reports/main/01234567_baseline_0
```

The directory name follows this rule:

```text
REPORT_ROOT/{BRANCH_SLUG}/{COMMIT8}_{NAME_SLUG}_{COUNT}
```

For example:

```text
$REPO/reports/main/01234567_baseline_0
```

`COMMIT8` is the first 8 hexadecimal characters of the recorded commit.
`NAME_SLUG` comes from `--name`, with unsafe path characters replaced by `-`.
`COUNT` starts at `0` and increments when a matching experiment directory already
exists.

Run that exact directory on the same or another machine with access to the
recorded repository:

```bash
runforge run "$REPO/reports/main/01234567_baseline_0"
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

Argument-array commands are the default. For a pipeline or other shell syntax,
pass one quoted command string with `--shell`:

```bash
runforge plan \
  --name train-evaluate \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  --shell -- \
  "python train.py --output '{ARTIFACT_DIR}' && python evaluate.py --weights '{ARTIFACT_DIR}/weights.pt'"
```

`{ARTIFACT_DIR}` is replaced by the planner with the experiment's
`artifacts/` directory before `config.json` is written. The worker executes the
rendered command; it does not substitute placeholders. The worker also provides
`RUNFORGE_ARTIFACT_DIR` to the child process.

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
REPORT_ROOT/
  BRANCH_SLUG/
    COMMIT8_NAME_SLUG_COUNT/
      config.json      # immutable rendered command and recorded Git source
      status.json      # mutable lifecycle/result state
      git.patch        # present when tracked changes differ from HEAD
      cmd.sh           # rendered command for inspection
      stdout.log
      stderr.log
      artifacts/
```

The lifecycle is `created -> init -> inprogress -> completed|failed`.

## Reproducibility Boundary

At planning time RunForge records the full current `HEAD` commit and branch
name, captures staged and unstaged tracked changes as a binary Git patch, and
lists untracked non-ignored paths. Untracked file contents are not copied. The
planner emits a warning when they are present, so do not make the planned command
depend on them.

At run time the worker creates a detached worktree at the recorded commit,
checks the stored patch SHA-256, applies the patch, then runs the recorded
command from that worktree. Outputs remain in the experiment directory, outside
the temporary worktree.

Use a project-specific report root. If that root is inside the source
repository, keep it ignored by Git so report files do not become untracked source
files on subsequent plans.

