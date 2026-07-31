# Matrices And Mapping

A matrix expands one source and one command template into an independent
experiment directory per parameter combination. This guide covers writing the
matrix file, reading the generated layout, and recovering which directory holds
which combination after the sweep has finished.

## Matrix File

A matrix file is a JSON object whose values are non-empty arrays of strings,
numbers, or booleans:

```json
{
  "LR": [0.1, 0.01],
  "SEED": [1, 2]
}
```

Null values are rejected. Every combination of every axis is planned, so the
example above produces four experiments.

## Planning A Sweep

```bash
runforge matrix \
  --name learning-rate-sweep \
  --matrix-file matrix.json \
  --out-dir "$REPORT_ROOT" \
  --source-path "$REPO" \
  -- python train.py --lr '{LR}' --seed '{SEED}' --output '{ARTIFACT_DIR}'
```

`matrix` uses current-HEAD mode by default. To sweep an explicit revision, add
`--source-mode pinned-git --commit v1.2.0`.

Parameter names are sorted to define axis order, and each array's own order is
preserved. The expansion is therefore deterministic: the same matrix file and
source always produce the same directories in the same order.

The source is resolved and validated once and shared by every combination, and
every combination is validated before anything is published. Planning never
executes a command.

## Matrix Consistency Checks

RunForge checks matrix combinations before publishing them. A declared axis
must affect the effective command or one of the rendered input files when its
values produce otherwise identical executions.

For example, this matrix declares two learning rates:

```json
{
  "LR": [0.1, 0.01],
  "SEED": [1, 2]
}
```

This command is unsafe because `--lr` is fixed:

```bash
runforge matrix --matrix-file matrix.json -- \
  python train.py --lr 0.1 --seed '{SEED}' --output '{ARTIFACT_DIR}'
```

RunForge rejects the plan because the combinations that differ only in `LR`
have the same effective command and input tree after generated artifact paths
are normalized. The error identifies the colliding combinations and differing
parameters. No experiment configuration is published.

Use the matrix placeholder to make the axis affect execution:

```bash
runforge matrix --matrix-file matrix.json -- \
  python train.py --lr '{LR}' --seed '{SEED}' --output '{ARTIFACT_DIR}'
```

Duplicate values such as `"SEED": [1, 1]` are also rejected when they produce
the same effective execution. A single-value axis may remain metadata-only; the
safety check is concerned with duplicate work, not whether every axis name
appears in the command.

Strict placeholders in argument-array commands and templated input files must
be declared matrix parameters or RunForge-owned values such as
`{ARTIFACT_DIR}` and `{INPUT_DIR}`. Shell commands are treated as opaque
scripts for placeholder discovery, so valid shell syntax such as
`awk '{print $1}'` remains unchanged.

## Reading The Generated Layout

Directory names identify a combination by position, not by value:

```text
REPORT_ROOT/main/a9f2ee3c_exp_0000
REPORT_ROOT/main/a9f2ee3c_exp_0001
```

Because that is not readable on its own, `matrix` prints a mapping table:

```text
Matrix configuration mapping:
index | dir_name          | LR    | SEED
------+-------------------+-------+-----
0000  | a9f2ee3c_exp_0000 | 0.001 | 1
0001  | a9f2ee3c_exp_0001 | 0.001 | 2
0002  | a9f2ee3c_exp_0002 | 0.01  | 1
0003  | a9f2ee3c_exp_0003 | 0.01  | 2
```

Values keep their JSON types, so a string parameter is quoted and a number is
not. This distinguishes the string `"1"` from the number `1`.

## The Persisted Mapping

The same mapping is written beside the generated directories:

```text
REPORT_ROOT/BRANCH_SLUG/COMMIT8_NAME_SLUG_matrix.json
```

```json
{
  "kind": "runforge_matrix_mapping",
  "schema_version": 1,
  "matrix_id": "main_a9f2ee3c_exp",
  "parameters": ["LR", "SEED"],
  "rows": [
    {"index": 0, "dir_name": "a9f2ee3c_exp_0000", "parameters": {"LR": 0.001, "SEED": 1}}
  ]
}
```

This is the canonical machine-readable record. Parameter values keep their JSON
types, so downstream analysis does not have to re-parse them.

Planning never overwrites an existing mapping. Each expansion reserves its own
filename, so re-planning the same matrix writes `..._matrix_0001.json` beside
the original, and two planners publishing into one report root cannot claim the
same name.

The mapping records plans that are already published, so failing to write it
does not fail the command. If the artifact cannot be saved, `matrix` still
reports every created directory and exits `0` with a warning on standard error.

## Reading The Mapping Later

Results are usually reviewed after a sweep finishes rather than while it is
planned. `matrix-show` renders a saved artifact at any later time:

```bash
runforge matrix-show "$REPORT_ROOT/main/a9f2ee3c_exp_matrix.json"
```

It only inspects: it never plans, executes, or changes experiment state, so it
is safe to run against a report root that workers are actively consuming. It
exits `0` after rendering, and `2` when the artifact is missing, malformed, or
written by an unsupported schema version.

## Matrix Parameters In Configuration Files

Placeholders work in rendered input trees as well as in commands, so a sweep can
drive programs that read JSON, YAML, or INI configuration rather than flags. See
[Planned configuration inputs](planned-configuration-inputs.md).

## Running The Sweep

Matrix planning produces ordinary experiment directories. Execute them with a
worker, which claims each one before running it so several workers can share a
report root safely:

```bash
runforge worker "$REPORT_ROOT"
```

See [Workers and recovery](workers-and-recovery.md).
