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

Consequently, a command that depends on an untracked wrapper will fail in the
detached worker worktree. The wrapper must be committed or staged before
planning. In pinned mode, it must exist in the selected commit or be included in
the explicit captured patch.

Committing stable wrappers is the preferred workflow. It keeps the Git index
untouched during planning and makes the pipeline available to other machines.

## Temporary Untracked Wrapper

For a deliberately local wrapper, a Make target can stage it immediately before
planning and restore it to the untracked state afterward. This is an advanced
workaround because it temporarily mutates the Git index.

```makefile
SHELL := /bin/bash
ENV_FILE ?= training.env

.PHONY: launch
launch:
	@set -euo pipefail; \
	script=run_pipeline.sh; \
	if ! git diff --cached --quiet -- "$$script"; then \
		echo "$$script already has staged changes; refusing to alter them"; \
		exit 2; \
	fi; \
	trap 'git reset --quiet -- "$$script"' EXIT; \
	git add -- "$$script"; \
	runforge launch \
		--name exp \
		--source-path . \
		--env-file "$(ENV_FILE)" \
		--stream-output \
		-- bash "./$$script"
```

Run it with:

```bash
make launch
```

The staged file is included in RunForge's captured patch. The `EXIT` trap
unstages it whether planning or execution succeeds, fails, or is interrupted.
The guard refuses to overwrite an existing staged version of the same file.
The target assumes that `make launch` is invoked from the source repository
root. Override `ENV_FILE` when the input file is elsewhere:

```bash
make launch ENV_FILE=/path/to/training.env
```

Do not run this target concurrently with another command that modifies the Git
index. The target does not isolate unrelated tracked changes; current-HEAD
planning intentionally captures the repository's complete tracked diff.

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
