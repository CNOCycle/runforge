# Non-Git Directory Sources

Not every study lives in a Git repository. A directory of scripts you are
iterating on, or a dataset-processing folder you never intend to commit, can
still be planned and executed reproducibly.

RunForge offers two modes for this. They differ in one decision: whether the
source is copied.

| | `verified-directory` | `directory-snapshot` |
| --- | --- | --- |
| Source stored in the experiment | No, only a manifest and the path | Yes, a complete copy |
| Executes from | The original directory, in place | An isolated temporary workspace |
| Survives the original being edited | No, execution fails | Yes |
| Portable to another machine | No | Yes |
| Storage cost per experiment | Manifest only | Full source tree |

Both modes ignore `.git/` and record no commit, branch, patch, or untracked-file
metadata, whether or not the directory happens to sit inside a repository.

## Shared Requirement: An External Output Root

For both modes `--out-dir` is required, and it must resolve outside
`--source-path`. Planning rejects an output root equal to or below the source
directory, which prevents reports from being captured into subsequent snapshots
of themselves.

A sibling directory is the recommended convention:

```text
/work/my-study/          <- source
/work/my-study-reports/  <- report root
```

## Verified Directory

Use this when the directory is local, fixed, and you do not want copies.

```bash
runforge plan \
  --name ablation \
  --source-mode verified-directory \
  --source-path /work/my-study \
  --out-dir /work/my-study-reports \
  -- python train.py --output '{ARTIFACT_DIR}'
```

Planning records the resolved absolute path, an ordered manifest of every
regular file with its SHA-256 and executable bit, and a full-tree digest.

Before every execution the worker requires the directory at the same path,
rescans its complete tree, and rejects any manifest or digest difference. A file
edited between planning and running is a failure, not a silent change of
meaning. RunForge cannot make the directory read-only, so commands that modify
their own source are your responsibility.

This mode is deliberately local and non-portable.

## Directory Snapshot

Use this when you want the experiment to be self-contained.

```bash
runforge plan \
  --name ablation \
  --source-mode directory-snapshot \
  --source-path /work/my-study \
  --out-dir /work/my-study-reports \
  -- python train.py --output '{ARTIFACT_DIR}'
```

Planning atomically captures the source below the experiment directory:

```text
EXPERIMENT_DIR/
  source/
  source-manifest.json
```

Before execution the worker validates manifest membership, digests, executable
bits, and the full-tree digest, materializes the verified source in an isolated
temporary workspace, runs there, preserves logs and artifacts in the experiment
directory, and removes the workspace. The original path is recorded only as
provenance; the worker does not need it.

Capture or verification failure leaves no partially published plan and never
starts the command.

## Ignore Rules

`directory-snapshot` applies an optional root `.gitignore` and excludes matching
entries. `verified-directory` instead *rejects* any entry the patterns would
exclude, because it executes the directory in place and cannot omit files from
it.

The supported subset is deliberate and documented rather than claiming full Git
compatibility: blank lines, comments, basename patterns, relative path patterns,
and trailing-slash directory patterns. Negation is not supported. `.git/` cannot
be re-included, and `.runforgeignore` has no meaning.

## File Type Restrictions

Both modes support regular files and directories and preserve executable bits.
Every symlink, socket, device, and FIFO is rejected. The full-tree digest is
independent of traversal order, timestamps, ownership, and the original absolute
path, so the same content produces the same identity anywhere.

## Layout

Non-Git plans use mode-specific bands and the source digest for identity:

```text
REPORT_ROOT/
  verified/
    HASH8_NAME_SLUG_COUNT/
  snapshot/
    HASH8_NAME_SLUG_COUNT/
```

`HASH8` is the first eight hexadecimal characters of the full-tree digest.

## Matrices

Both modes work with `matrix`. The source is scanned and validated once, then
shared across every combination. Snapshot plans remain self-contained even when
that duplicates a small tree for each experiment.

```bash
runforge matrix \
  --name ablation \
  --matrix-file matrix.json \
  --source-mode directory-snapshot \
  --source-path /work/my-study \
  --out-dir /work/my-study-reports \
  -- python train.py --seed '{SEED}' --output '{ARTIFACT_DIR}'
```
