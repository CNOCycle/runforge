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
Regardless of streaming mode, `run`, `launch`, `retry`, and discovery execution
print flushed preparation, execution, and final success or failure messages so
a quiet child process is distinguishable from a stalled CLI. In log-only mode,
the lifecycle messages also identify the active `stdout.log` and `stderr.log`
paths.

## Retry A Failed Or Interrupted Experiment

Retry starts another execution of the same immutable experiment configuration;
it does not resume the previous process or select a training checkpoint:

```bash
runforge retry --stream-output "$REPO/reports/main/01234567_baseline_0000"
```

A normal retry accepts a `failed` experiment. An experiment left in
`inprogress` after an interruption requires explicit confirmation that the old
process has stopped:

```bash
runforge retry --force --stream-output \
  "$REPO/reports/main/01234567_baseline_0000"
```

`--force` is a single-controller escape hatch. RunForge cannot yet prove that
another worker is inactive, so using it while the original process is still
running can execute the experiment twice. Completed experiments cannot be
retried; create a new plan for another intentional successful run. Experiments
in `created` or `init` remain runnable with `run`.

Before execution, retry archives the previous `status.json`, logs, and artifacts
under `attempt-NNNN/`, creates an empty `artifacts/` directory, and
resets the status for the normal worker. The worker increments `attempt` when
the new command starts.

## Pinned Git Source

Use pinned mode when the intended source is an explicit commit or ref rather
than the repository's current checkout. `--patch` is optional:

```bash
runforge plan \
  --name release-baseline \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  --source-mode pinned-git \
  --commit v1.2.0 \
  --patch /path/to/change.patch \
  -- python train.py --output '{ARTIFACT_DIR}'
```

RunForge resolves the ref to a full commit, validates the captured patch bytes
in a detached worktree at that commit, and stores the patch plus its SHA-256 in
the plan. Pinned plans are placed under the `pinned/` report branch slug.

## Parameter Matrices

Matrix planning takes one Git source (current-HEAD or pinned, same as `plan`)
and a JSON object whose values are non-empty arrays of strings, numbers, or
booleans:

```json
{
  "LR": [0.1, 0.01],
  "SEED": [1, 2]
}
```

```bash
runforge matrix \
  --name learning-rate-sweep \
  --matrix-file matrix.json \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  -- python train.py --lr '{LR}' --seed '{SEED}' --output '{ARTIFACT_DIR}'
```

`matrix` uses current-HEAD mode by default. To sweep an explicit revision, pass
`--source-mode pinned-git --commit v1.2.0`. Parameter names are sorted to define
axis order, while each array's value order is preserved. Every Cartesian
combination receives its own normal experiment directory and a rendered
command; the selected values are also stored in `config.json` under
`parameters`. The CLI prints every created directory.

Matrix planning resolves and validates the source once and shares that single
resolved commit (and, in current-HEAD mode, the same captured patch and
untracked-file warning) across every combination, validates every combination
before publication, and does not execute any experiment. Use
`runforge run EXPERIMENT_DIRECTORY` for each resulting plan.

## Non-Git Directory Sources

`--source-mode verified-directory` and `--source-mode directory-snapshot` plan
a directory directly, whether or not it sits inside a Git repository. Neither
mode captures a Git commit, branch, patch, or untracked-file list. `--out-dir`
is required for both and must resolve outside `--source-path`:

```bash
runforge plan --source-mode verified-directory \
  --source-path "$SOURCE_DIR" --out-dir "$SOURCE_DIR-reports" \
  -- python train.py --out '{ARTIFACT_DIR}'
```

`verified-directory` records only a manifest and re-verifies the live
directory at the same path before every run. `directory-snapshot` copies the
source tree into the experiment directory at plan time, so the original
directory can be moved or deleted afterward. Both support `matrix` the same
way Git sources do, sharing one validated source identity across every
combination. See the
[non-Git sources guide](docs/guides/non-git-sources.md) for the full
contract, ignore rules, and layout.

## Discover Planned Experiments

Inspect every RunForge experiment below a report root with:

```bash
runforge discover "$REPORT_ROOT"
```

The root defaults to the current directory when omitted. Discovery scans
recursively without following directory symlinks and prints one experiment per
line in deterministic path order. Each line includes its state, attempt, name,
source identifier, and experiment path. A summary reports counts for
`created`, `init`, `inprogress`, `completed`, `failed`, and invalid candidates.

Without `--execute`, this command is read-only. It does not execute experiments
or modify their status. Missing or malformed `config.json`/`status.json` pairs
are reported individually, and the command returns status `2` when any are
found.

Add `--execute` to run every discovered plan whose status is `created`:

```bash
runforge discover "$REPORT_ROOT" --execute
```

The command takes one discovery snapshot, runs eligible experiments
sequentially in deterministic path order using the normal worker, and continues
after individual failures. Plans in `init`, `inprogress`, `completed`, or
`failed` remain visible but are skipped. Plans published after the scan wait
for the next invocation.

Use `--max-tasks N` with `--execute` to bound one worker invocation. `N` must
be positive; the command starts at most `N` eligible experiments and reports any
remaining created plans as deferred. Without `--max-tasks`, all eligible plans
are attempted. The option is invalid in read-only discovery mode.

By default, child output remains in each experiment's `stdout.log` and
`stderr.log`. Add `--stream-output` with `--execute` to also show it in the
console:

```bash
runforge discover "$REPORT_ROOT" --execute --stream-output
```

The execution summary reports selected, completed, failed, skipped, and invalid
counts. Exit status is `0` when every selected plan succeeds, `1` when any
selected plan fails, and `2` when discovery or metadata is invalid. Valid
created plans are still attempted when another candidate is invalid.

This first executor is intended for one controlling process. It does not claim
plans atomically, so do not run concurrent `discover --execute` processes
against the same report root.

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
REPORT_ROOT/
  BRANCH_SLUG/
    COMMIT8_NAME_SLUG_COUNT/
      config.json      # immutable command, parameters, environment, and source
      status.json      # mutable lifecycle/result state
      git.patch        # present when captured source includes a patch
      cmd.sh           # rendered command for inspection
      stdout.log
      stderr.log
      artifacts/
      attempt-NNNN/    # prior retry status snapshot, logs, and artifacts
```

`verified-directory` and `directory-snapshot` plans use `verified/` and
`snapshot/` report bands keyed by the source's full-tree digest instead of a
Git branch and commit, and store `source-manifest.json` (plus a captured
`source/` tree for `directory-snapshot`) instead of `git.patch`. See the
[non-Git sources guide](docs/guides/non-git-sources.md) for that layout.

The lifecycle is `created -> init -> inprogress -> completed|failed`.

## Reproducibility Boundary

In current-HEAD mode RunForge records the full commit and branch, captures staged
and unstaged tracked changes as a binary Git patch, and lists untracked
non-ignored paths. Untracked file contents are not copied. The planner warns
when they are present, so do not make the command depend on them.

In pinned mode RunForge resolves only the supplied repository and commit/ref. It
does not infer source from the current checkout. An optional external patch is
captured, hashed, and checked against a detached worktree at the resolved commit.

`matrix` resolves its source exactly once, before expanding any combination, and
every resulting plan shares that same resolved commit and patch identity. In
current-HEAD mode this means the whole sweep is anchored to one `HEAD` snapshot
taken at matrix-planning time, not re-read per combination.

At run time the worker creates a detached worktree at the recorded commit,
checks the stored patch SHA-256, applies the patch, then runs the recorded
command from that worktree. Outputs remain in the experiment directory, outside
the temporary worktree.

Use a project-specific report root. If that root is inside the source
repository, keep it ignored by Git so report files do not become untracked source
files on subsequent plans.

## License

RunForge is licensed under the
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
