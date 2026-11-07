# Long Pipeline Commands

Long training pipelines often contain many fixed model and optimization options
but only a few run-specific values. Keeping the entire pipeline in a RunForge
command makes planning difficult to read and makes shell quoting part of the
experiment interface.

The recommended pattern is:

1. keep the fixed pipeline in a project-owned wrapper script or configuration
   profile;
2. keep changing, non-secret inputs in an explicit environment file;
3. execute the wrapper as an argument-array command; and
4. write all generated output below `RUNFORGE_ARTIFACT_DIR`.

Where the wrapper lives decides how you launch it:

- **Part of the project:** commit it (or at least `git add` it) and run it from
  the source. This is the recommended default.
- **Local only:** keep it in a separate folder and capture that folder with
  `--input-tree`. See
  [Run a Local Wrapper Without Committing It](#run-a-local-wrapper-without-committing-it).

## Recommended Tracked Wrapper

Create `experiments/run_pipeline.sh` in the source repository:

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${TRAIN_DATA:?TRAIN_DATA is required}"
: "${VALIDATION_DATA:?VALIDATION_DATA is required}"
: "${SEED:?SEED is required}"
ARTIFACT_DIR="${RUNFORGE_ARTIFACT_DIR:?RUNFORGE_ARTIFACT_DIR is required}"

python3 scripts/train_model.py \
  --input "$TRAIN_DATA" \
  --validation "$VALIDATION_DATA" \
  --seed "$SEED" \
  --epochs 20 \
  --learning-rate 0.001 \
  --output_dir "$ARTIFACT_DIR"

python3 scripts/evaluate_model.py \
  --config "$ARTIFACT_DIR/model-config.json" \
  --checkpoint "$ARTIFACT_DIR/checkpoint.bin" \
  --input "$VALIDATION_DATA" \
  --output_dir "$ARTIFACT_DIR/evaluation"
```

`set -euo pipefail` stops the wrapper when training fails, so inference is not
started with incomplete outputs. The fixed options are reviewed and captured as
part of the source state instead of being repeated for every RunForge command.

Commit the stable wrapper:

```bash
git add experiments/run_pipeline.sh
git commit -m "Add training and inference pipeline"
```

An uncommitted wrapper is also captured in current-HEAD mode after it has been
staged, because the resulting new tracked file is part of `git.patch`.

## Run-Specific Inputs

Place changing values in an environment file, for example `training.env`:

```dotenv
TRAIN_DATA=/data/example/train.csv
VALIDATION_DATA=/data/example/validation.csv
SEED=42
```

Then launch the pipeline:

```bash
REPO=/path/to/your-project
ENV_FILE=/path/to/training.env

runforge launch \
  --name demo-pipeline \
  --source-path "$REPO" \
  --env-file "$ENV_FILE" \
  --stream-output \
  -- bash experiments/run_pipeline.sh
```

This invocation does not need `--shell`. RunForge records an argument-array
command containing `bash` and the wrapper path; Bash interprets the pipeline
inside the source worktree.

RunForge persists explicit environment override values in `config.json`. Do not
put credentials or other secrets in this file. Dataset paths identify locations
but do not capture dataset contents; reproducible data versioning or checksums
remain the project's responsibility.

## Run a Local Wrapper Without Committing It

If the wrapper is a local file that should not go into Git, put it in its own
folder and capture that folder with `--input-tree`:

```text
pipeline-inputs/
  run_pipeline.sh
```

Use the same wrapper as above. Then launch the captured copy, which RunForge
exposes as `{INPUT_DIR}`:

```bash
runforge launch \
  --name demo-pipeline \
  --source-path "$REPO" \
  --input-tree /path/to/pipeline-inputs \
  --env-file "$ENV_FILE" \
  --stream-output \
  -- bash '{INPUT_DIR}/run_pipeline.sh'
```

The wrapper does not need to be staged or committed. Keep in mind:

- The command still runs in the source directory, so `scripts/train_model.py`
  and `scripts/evaluate_model.py` must exist in the selected source.
- Every file in the folder is captured when the experiment is planned, not
  only the wrapper. Files must be UTF-8 regular files, and symbolic links are
  rejected. Keep unrelated files out of the folder.
- JSON, YAML, and INI files are rendered as templates, so placeholders such as
  `{ARTIFACT_DIR}` are replaced and an invalid file or unknown placeholder can
  make planning fail.
- Other files, including `.sh`, are not rendered: placeholders such as
  `{INPUT_DIR}` are not substituted inside the wrapper. They are read as UTF-8
  text, so line endings may be normalized (a CRLF script is not guaranteed to
  be captured byte-for-byte). Use LF line endings.
- Do not put secrets in the folder. It is recorded with the experiment.
- If the folder is inside the Git repository, RunForge may warn that the
  untracked wrapper is not in the Git patch. The input capture includes it.

To fill `{ARTIFACT_DIR}` placeholders in configuration files, see
[Planned configuration inputs](planned-configuration-inputs.md).

## Artifact Directory Forms

Inside a wrapper, use the environment variable provided to the child process:

```bash
ARTIFACT_DIR="${RUNFORGE_ARTIFACT_DIR:?}"
```

For a command written directly on the RunForge command line, use the planner
placeholder:

```text
{ARTIFACT_DIR}
```

Do not use `${ARTIFACT_DIR}` in a RunForge command. It is neither the documented
placeholder nor the environment variable provided by the worker.

## Source Capture Boundary

In current-HEAD mode, RunForge captures:

- the selected commit;
- staged and unstaged changes to tracked files; and
- the names, but not contents, of untracked non-ignored files.

Consequently, a command that accesses an untracked wrapper in the source
worktree will fail. For that command, the wrapper must be committed or staged
before planning. In pinned mode, it must exist in the selected commit or be
included in the explicit captured patch. The input-tree invocation in
[Run a Local Wrapper Without Committing It](#run-a-local-wrapper-without-committing-it)
instead accesses its separately captured copy and needs neither staging nor a
commit for the wrapper.

Committing stable wrappers is the preferred workflow. It keeps the Git index
untouched during planning and makes the pipeline available to other machines.

## Configuration Profiles

When several pipelines share one driver, put fixed option sets in tracked TOML,
YAML, or JSON profiles and let the wrapper select one profile. The RunForge
command can remain short while the profile and wrapper stay reviewable inside
the captured source state.

RunForge does not need a domain-specific preset system for this pattern. Its
responsibility is to capture the source, explicit environment, command, and
artifact destination used by the project-owned pipeline.

## When A Pipeline Fails

`set -euo pipefail` makes the wrapper stop at the first failing step, so a later
step never runs on incomplete output. RunForge records the wrapper's exit code
in `status.json`, marks the experiment `failed`, and keeps the logs and partial
artifacts for diagnosis.

After fixing the cause, retry starts a fresh attempt of the same recorded
pipeline. It does not resume a partially finished pipeline, so a wrapper that
should skip completed stages must implement that behavior itself, usually by
checking its own outputs under `RUNFORGE_ARTIFACT_DIR`.

See [Workers and recovery](workers-and-recovery.md) for retry state eligibility,
claim handling, `--force`, and the general worker recovery rules.
