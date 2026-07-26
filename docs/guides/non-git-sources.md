# Non-Git Directory Sources

RunForge's default `current-head` and `pinned-git` source modes require the
selected directory to be inside a Git repository. `--source-mode
verified-directory` and `--source-mode directory-snapshot` instead plan a
simple-script directory directly, whether or not it happens to sit inside a
Git repository. Both modes ignore `.git/`, apply an optional
`.runforgeignore`, treat tracked and untracked files identically, and record
no Git commit, branch, patch, or untracked-file metadata.

```bash
runforge plan --source-mode verified-directory \
  --source-path "$SOURCE_DIR" --out-dir "$SOURCE_DIR-reports" \
  -- python train.py --out '{ARTIFACT_DIR}'

runforge plan --source-mode directory-snapshot \
  --source-path "$SOURCE_DIR" --out-dir "$SOURCE_DIR-reports" \
  -- python train.py --out '{ARTIFACT_DIR}'
```

For both modes, `--out-dir` is required and must resolve outside
`--source-path`; planning rejects an output root equal to or below the source
directory. This keeps non-Git storage intentional and avoids recursively
capturing the report root into its own source scan. A sibling directory such
as `SOURCE_NAME-reports` is a convenient local convention since the Git-backed
default of `SOURCE_REPOSITORY/reports` does not apply here.

Both modes support regular files and directories, preserve executable bits,
and reject every symlink, socket, device, FIFO, and other special file
anywhere in the tree.

## Choosing a mode

**`verified-directory`** is for one local node and a source directory you
treat as fixed. Nothing is copied into the experiment directory; only a
manifest of relative paths, executable bits, and SHA-256 digests is recorded
in `source-manifest.json`. Before every execution, the worker requires the
recorded directory at the same path, rescans it under the recorded ignore
rules, and refuses to run if anything has changed. The command then runs
directly from that directory. RunForge cannot make the directory read-only,
so a command that modifies its own source is the user's responsibility. This
mode is deliberately local and non-portable — there is no unverified
live-directory option.

**`directory-snapshot`** is the portable, self-contained alternative. Planning
copies the source tree into `EXPERIMENT_DIR/source/` alongside
`source-manifest.json`; the original directory is never required again and
may be moved, edited, or deleted right after planning. Before every execution,
the worker verifies the captured tree against the manifest, copies it into an
isolated temporary workspace, runs the command there, and removes the
workspace afterward — the captured `source/` tree itself is never executed in
place and is never modified by the command.

## Ignoring files

`.git/` is always excluded, at any depth, and cannot be re-included. An
optional `.runforgeignore` file directly below the source directory adds
further exclusions: each non-blank, non-comment line is a glob pattern. A
pattern containing `/` matches the full relative POSIX path from the source
root; a pattern without `/` matches the basename at any depth. A trailing `/`
is stripped before matching. There is no negation syntax.

```text
# .runforgeignore
*.log
__pycache__/
scratch/
```

## Matrix expansion

`matrix` supports both modes the same way it supports Git sources: the source
directory is scanned (or captured) exactly once, and every parameter
combination gets its own experiment directory sharing that one validated
identity.

```bash
runforge matrix --source-mode verified-directory \
  --source-path "$SOURCE_DIR" --out-dir "$SOURCE_DIR-reports" \
  --matrix-file matrix.json \
  -- python train.py --seed '{SEED}' --out '{ARTIFACT_DIR}'
```

Each combination in a `directory-snapshot` matrix gets its own copy of the
captured source tree; storage deduplication across combinations is deferred
until demonstrated necessary.

## Layout

Non-Git plans use mode-specific layout bands and the full-tree digest for
identity, instead of the Git-backed branch/commit layout:

```text
REPORT_ROOT/
  verified/
    HASH8_NAME_SLUG_COUNT/
      config.json
      status.json
      source-manifest.json
      cmd.sh
      stdout.log
      stderr.log
      artifacts/
  snapshot/
    HASH8_NAME_SLUG_COUNT/
      config.json
      status.json
      source-manifest.json
      source/
      cmd.sh
      stdout.log
      stderr.log
      artifacts/
```

`HASH8` is the first eight hexadecimal characters of the full-tree digest,
which is independent of traversal order, timestamps, and user/group IDs.

`discover` lists non-Git plans the same way as Git-backed plans, showing
`verified@HASH8` or `snapshot@HASH8` in place of a Git branch and commit.
