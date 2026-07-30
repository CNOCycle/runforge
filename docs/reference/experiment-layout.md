# Experiment Layout

An experiment directory is self-contained: everything needed to reconstruct,
execute, and audit one run lives inside it.

## Git-Backed Layout

```text
REPORT_ROOT/
  BRANCH_SLUG/
    COMMIT8_NAME_SLUG_COUNT/
      config.json          # immutable command, parameters, environment, source
      status.json          # mutable lifecycle and result state
      git.patch            # present when the captured source includes a patch
      cmd.sh               # rendered command, for inspection
      stdout.log
      stderr.log
      artifacts/
      inputs/              # rendered immutable input tree, when requested
      input-manifest.json  # input kinds and SHA-256 digests
      claim/               # present only while a worker owns the experiment
      attempt-NNNN/        # archived prior attempt, created by retry
```

`COMMIT8` is the first eight hexadecimal characters of the recorded commit.
`NAME_SLUG` comes from `--name` with unsafe characters replaced. `COUNT` starts
at `0000` and increments when a matching directory already exists.

## Non-Git Layout

`verified-directory` and `directory-snapshot` plans use their own report bands,
keyed by the source's full-tree digest rather than a branch and commit:

```text
REPORT_ROOT/
  verified/
    HASH8_NAME_SLUG_COUNT/
      source-manifest.json
  snapshot/
    HASH8_NAME_SLUG_COUNT/
      source-manifest.json
      source/              # the captured copy
```

They store `source-manifest.json` instead of `git.patch`. See
[Source modes](source-modes.md).

## Matrix Artifacts

A matrix expansion writes one mapping beside the directories it created, in the
band directory rather than inside any experiment:

```text
REPORT_ROOT/BRANCH_SLUG/COMMIT8_NAME_SLUG_matrix.json
```

See [Matrices and mapping](../guides/matrices-and-mapping.md).

## Who Writes What

| Path | Written by | Mutable |
| --- | --- | --- |
| `config.json` | Planner, once | No |
| `inputs/`, `input-manifest.json` | Planner, once | No |
| `source/`, `source-manifest.json` | Planner, once | No |
| `git.patch` | Planner, once | No |
| `status.json` | Planner, then worker | Yes |
| `stdout.log`, `stderr.log` | Worker | Replaced per attempt |
| `artifacts/` | The child command | Yes |
| `claim/` | Worker, while executing | Created and removed |
| `attempt-NNNN/` | Retry | Written once per attempt |

The immutable files are the experiment's identity. A worker verifies every
planned input digest and, for non-Git modes, the complete source manifest before
starting the command; a mismatch is a failure, not a silent change.

## Lifecycle

```text
created -> init -> inprogress -> completed
                         `----> failed
```

| State | Meaning |
| --- | --- |
| `created` | Planned, never started |
| `init` | Claimed and prepared, command not yet started |
| `inprogress` | Command running, or its worker died |
| `completed` | Command exited `0` |
| `failed` | Command exited non-zero, or preparation failed |

`created` and `init` are runnable. `inprogress` with no live worker requires
`retry --force`; see
[Workers and recovery](../guides/workers-and-recovery.md).

## Claim Files

```text
claim/owner.json
```

```json
{
  "kind": "runforge_experiment_claim",
  "schema_version": 1,
  "token": "231b9fedb23e46d4bdf71404ad3988cb",
  "owner": "node7:48213",
  "acquired_at": "2026-07-30T09:15:02.123456Z"
}
```

The directory's existence is the lock — it is created with an exclusive
operation, so at most one process can hold it. `token` is a random identifier
that fences every status write; `owner` is diagnostic and names the process to
check before forcing recovery.
