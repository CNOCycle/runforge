# Reproducibility Boundary

RunForge captures a specific, bounded set of things. Knowing where that boundary
falls is the difference between an experiment you can reconstruct and one you
merely have logs for.

## What Is Captured

**Source identity.** In `current-head` mode: the full commit and branch, plus
staged and unstaged changes to tracked files as a binary Git patch. In
`pinned-git` mode: only the supplied repository and resolved commit, never
inferred from the current checkout, plus any external patch, hashed and
validated against a detached worktree at that commit. In the non-Git modes: a
manifest of every regular file with digests and executable bits, and a full-tree
digest independent of traversal order, timestamps, ownership, and absolute path.

**The command**, as an argument array with all planner placeholders already
substituted.

**Explicitly requested environment overrides**, and only those.

**Rendered configuration inputs**, with SHA-256 digests verified before every
run.

## What Is Not Captured

**Untracked file contents.** `current-head` records untracked non-ignored paths
by *name* and warns about them. A command depending on one will fail in the
worker's detached worktree.

**The ambient environment.** RunForge never snapshots `os.environ`. A value you
did not pass to `--env-file` is not part of the record, even if it was set when
you planned.

**Data.** Dataset paths identify locations, not contents. Versioning or
checksumming your data remains your responsibility.

**The interpreter and installed packages.** RunForge captures your source, not
your Python environment. Pin that separately — a lockfile inside the captured
source is the usual approach.

## Matrix Anchoring

`matrix` resolves its source exactly once, before expanding any combination, and
every plan shares that resolved identity. In `current-head` mode the whole sweep
is anchored to one `HEAD` snapshot taken at planning time, not re-read per
combination. Editing the working tree midway through a large expansion cannot
split it across two source states.

## At Run Time

The worker reconstructs the recorded source, verifies every digest it holds, and
only then starts the command. Verification failure prevents execution rather
than producing a run whose provenance is wrong.

Outputs stay in the experiment directory, outside any temporary worktree, so
they survive cleanup.

## Report Root Placement

Use a project-specific report root. If it sits inside the source repository,
keep it ignored by Git, or report files become untracked source files that the
next plan warns about.
