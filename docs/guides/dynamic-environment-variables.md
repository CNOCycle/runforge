# Dynamic Environment Variables

Some values cannot be written into a static environment file because they are
not known until RunForge has chosen where the experiment will run. The path of
the detached worktree, the artifact directory, and the rendered input directory
are all decided per experiment.

This guide explains which mechanism to use for each kind of value, and why
mixing them silently produces the wrong path.

## Four Mechanisms, Four Jobs

| Value depends on | Use | Resolved |
| --- | --- | --- |
| Nothing; it is a fixed input | `--env-file` | At planning, stored in `config.json` |
| The experiment directory | `{ARTIFACT_DIR}` / `{INPUT_DIR}` | At planning, substituted into the recorded command and input templates |
| The experiment directory, inside a script | `$RUNFORGE_ARTIFACT_DIR` / `$RUNFORGE_INPUT_DIR` | At execution, exported to the child process |
| The runtime working directory | Shell expansion inside a wrapper | At execution, by the shell |

## Placeholders Are Not Environment Variables

This is the distinction that causes the most confusion, so it is worth stating
directly.

`{ARTIFACT_DIR}` is a **planning-time placeholder**. The planner substitutes it
into two places only: the command you write after `--`, and the contents of
files rendered through `--input-tree`. The substituted value is baked into
`config.json` before the experiment ever runs.

`RUNFORGE_ARTIFACT_DIR` is a **runtime environment variable**. The worker exports
it to the child process immediately before execution.

They are not interchangeable. A wrapper script is *source*, not a template, so
the planner never looks inside it:

```bash
# WRONG: inside run_pipeline.sh. Nothing substitutes this, so the program
# receives the literal text and creates a directory named {ARTIFACT_DIR}.
python train.py --output_dir "{ARTIFACT_DIR}/training"

# CORRECT: inside run_pipeline.sh
python train.py --output_dir "$RUNFORGE_ARTIFACT_DIR/training"
```

The reverse mistake fails in one of two ways, depending on your quoting:

```bash
# WRONG, and caught: your shell expands the unset variable to an empty string
# before RunForge sees it, and planning refuses with
#   command.arguments must be an array of non-empty strings
runforge plan --source-path "$REPO" -- python train.py --output "$RUNFORGE_ARTIFACT_DIR"

# WRONG, and quiet: the literal text is recorded. Argument-array commands are
# never shell-evaluated, so the worker passes those 24 characters to the program.
runforge plan --source-path "$REPO" -- python train.py --output '$RUNFORGE_ARTIFACT_DIR'

# CORRECT
runforge plan --source-path "$REPO" -- python train.py --output '{ARTIFACT_DIR}'
```

Quote the placeholder with single quotes so your shell does not try to expand
the braces before RunForge sees them. The recorded command in `config.json`
shows exactly what the worker will run, so it is worth reading once after
planning if a path looks wrong.

## Values Derived From The Runtime Directory

A worker executes the recorded command from a detached worktree whose path is
chosen per run. Anything relative to that directory must be derived at runtime,
inside a wrapper:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Resolved when the worker runs this script, in whichever worktree it created.
export MY_DATA_DIR="$(pwd -P)/data"
export TRAIN_CONFIG="$RUNFORGE_INPUT_DIR/train.json"
export EVAL_CONFIG="$RUNFORGE_INPUT_DIR/eval.json"
export OUTPUT_DIR="$RUNFORGE_ARTIFACT_DIR"

python train.py
```

Launch it as an argument array; no `--shell` is required because `bash` runs the
script:

```bash
runforge launch --name baseline --source-path "$REPO" -- bash experiments/run_pipeline.sh
```

## Linked Configuration Files

When a training program and an evaluation program must agree on a checkpoint
path, deriving that path twice in shell is fragile. Render both files from one
placeholder set instead:

```bash
runforge plan --source-path "$REPO" --input-tree "$REPO/configs" -- python run_pipeline.py '{INPUT_DIR}/train.json' '{INPUT_DIR}/eval.json'
```

Both templates receive the same `{ARTIFACT_DIR}`, so the checkpoint written by
training is exactly the checkpoint evaluation reads. See
[Planned configuration inputs](planned-configuration-inputs.md) for the template
formats and the integrity guarantees.

## Static Overrides

Values that are genuinely fixed for a run belong in an environment file, which
RunForge records in `config.json`:

```dotenv
TRAIN_CSV=/datasets/project/train.csv
VAL_CSV=/datasets/project/validation.csv
```

```bash
runforge launch --name baseline --source-path "$REPO" --env-file training.env -- bash experiments/run_pipeline.sh
```

Only variables you name this way are recorded. RunForge never snapshots the
ambient environment, so a value you did not pass explicitly is not part of the
experiment record even if it was set in your shell.

## Secrets

Do not put credentials in environment files, command arguments, input
templates, or `RUNFORGE_CLAIM_OWNER`. All four are written to disk in the
experiment record or printed in console output. Supply secrets to the child
process through your own mechanism — a secret manager, or a variable exported in
the worker's environment but never passed to `--env-file`.
