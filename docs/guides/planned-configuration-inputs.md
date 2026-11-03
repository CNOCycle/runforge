# Planned Configuration Inputs

RunForge normally renders `{ARTIFACT_DIR}` in the recorded command. Some
pipelines instead receive all paths through one or more configuration files. For
example, a training configuration may choose an output directory while an
evaluation configuration must name the training checkpoint. Editing both files
for every planned experiment is error-prone and does not leave one immutable
record of the effective settings.

`--input-tree DIRECTORY` captures a UTF-8 configuration tree when a plan is
created. The planner copies it below the experiment's `inputs/` directory:

- `.json` files are parsed and rendered as JSON templates;
- `.yaml` and `.yml` files render as layout-preserving text templates and receive YAML syntax validation;
- `.ini` files render as layout-preserving text templates and receive `configparser` syntax validation;
- other UTF-8 regular files are copied byte-for-byte; and
- relative paths inside the tree remain unchanged.

The worker verifies the recorded input manifest before preparing source or
starting the command. It exposes the rendered input root as both `{INPUT_DIR}`
in the planned command and `RUNFORGE_INPUT_DIR` in the child environment.

## Linked Train And Evaluate Files

Author the source templates once. A complete string placeholder preserves the
selected JSON scalar type, while an embedded placeholder produces a JSON string.

`configs/train.json`:

```json
{
  "output_directory": "{ARTIFACT_DIR}/training",
  "checkpoint_name": "checkpoint.pt",
  "epochs": 50
}
```

`configs/eval.json`:

```json
{
  "checkpoint": "{ARTIFACT_DIR}/training/checkpoint.pt",
  "output_directory": "{ARTIFACT_DIR}/evaluation"
}
```

Plan and launch the pipeline with one shared input tree:

```bash
runforge launch \
  --name baseline \
  --source-path "$REPO" \
  --input-tree "$REPO/configs" \
  --shell -- \
  "python train.py '{INPUT_DIR}/train.json' && python evaluate.py '{INPUT_DIR}/eval.json'"
```

The resulting experiment contains the rendered configuration files, their
SHA-256 digests in `input-manifest.json`, logs, and artifacts. Training and
evaluation agree because both paths are derived from that experiment's single
`artifacts/` directory.

The literal `50` above stays a JSON number; the [Matrices](#matrices) section
shows an exact `"{SEED}"` placeholder keeping its JSON type.

## YAML And INI Files

YAML, YML, and INI use the same one-pass placeholder names without
normalizing or rewriting the file. Comments, whitespace, ordering, and
format-specific syntax remain intact. After rendering, RunForge validates YAML
and YML with PyYAML's composition parser and validates INI with Python's
`configparser`; it does not interpret the application's configuration schema:

```yaml
# configs/train.yaml
output_directory: "{ARTIFACT_DIR}/training"
epochs: {EPOCHS}
```

```ini
; configs/eval.ini
[evaluation]
checkpoint = {ARTIFACT_DIR}/training/checkpoint.pt
```

For the YAML example, supply `EPOCHS` as a matrix parameter; for a single plan,
replace `{EPOCHS}` with a fixed value. Numbers become stable text such as `50`,
and booleans become `true` or `false`. The template author supplies any quoting
required by the target format.

## Matrices

Matrix values are available to every template. For a separate sweep, use this
as `configs/train.json`. In JSON, an exact placeholder keeps the JSON type, so
a number or boolean does not become a string:

```json
{
  "learning_rate": "{LR}",
  "seed": "{SEED}",
  "amp": "{AMP}"
}
```

```json
{
  "LR": [0.01, 0.001],
  "SEED": [1, 2],
  "AMP": [true]
}
```

```bash
runforge plan \
  --name sweep \
  --source-path "$REPO" \
  --source-mode pinned-git \
  --commit HEAD \
  --matrix matrix.json \
  --input-tree "$REPO/configs" \
  -- python train.py '{INPUT_DIR}/train.json'
```

`ARTIFACT_DIR` and `INPUT_DIR` are reserved RunForge placeholders and cannot
be matrix parameter names. Unknown placeholders, invalid JSON/YAML/INI,
unsafe paths, symbolic links, non-UTF-8 files, and changed input files fail
before a child command can run. Do not put secrets in a planned input tree or
`--env-file`: both are recorded on disk. Supply them through your own secret
manager or the worker's environment, as described in
[Dynamic environment variables](dynamic-environment-variables.md#secrets).
