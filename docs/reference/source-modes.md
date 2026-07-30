# Source Modes

`--source-mode` selects how RunForge captures the code an experiment runs. Four
modes exist; they differ in what is recorded and what is verified before the
command starts.

| Mode | Records | Verified before every run |
| --- | --- | --- |
| `current-head` (default) | Commit, branch, tracked diff as `git.patch`, untracked file *names* | Commit resolves; patch digest matches |
| `pinned-git` | Explicit commit, optional external patch and its digest | Commit resolves; patch digest matches |
| `verified-directory` | Absolute path, file manifest, full-tree digest | The live directory still matches the manifest exactly |
| `directory-snapshot` | A captured copy, manifest, full-tree digest | The captured copy matches; then materialized in isolation |

## current-head

The default. Captures the repository's current commit plus staged and unstaged
changes to tracked files.

Untracked files are recorded by name and warned about, but **their contents are
not captured**. A command depending on an untracked file will fail in the
worker's detached worktree. Commit or stage it before planning. See
[Long pipeline commands](../guides/long-pipelines.md).

## pinned-git

Plans from an explicit commit or ref without consulting the current checkout:

```bash
runforge plan --name release-baseline --source-path "$REPO" --source-mode pinned-git --commit v1.2.0 --patch /path/to/change.patch -- python train.py --output '{ARTIFACT_DIR}'
```

The ref is resolved to a full commit, and any patch is validated in a detached
worktree at that commit before publication. Pinned plans land under the
`pinned/` band.

## verified-directory and directory-snapshot

For sources that are not Git repositories. Both ignore `.git/` and record no
commit, branch, or patch, whether or not the directory sits inside a repository.
Both require `--out-dir` to resolve outside `--source-path`.

See [Non-Git directory sources](../guides/non-git-sources.md) for the full
comparison, ignore rules, and file-type restrictions.

## Execution Environment

Git-backed modes execute from a detached worktree created per run and removed
afterwards. `verified-directory` executes the original directory in place.
`directory-snapshot` materializes its captured copy in an isolated temporary
workspace and removes it after the run.

In every mode the worker exports `RUNFORGE_ARTIFACT_DIR` and
`RUNFORGE_INPUT_DIR`, and writes output only below the experiment directory.
