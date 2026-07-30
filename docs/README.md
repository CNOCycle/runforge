# RunForge Documentation

Start with the [README](../README.md) to install RunForge and run one
experiment. This index lists everything else.

## Guides

Task-oriented pages for work you repeat. Each answers one question.

| Guide | Read it when |
| --- | --- |
| [Long pipeline commands](guides/long-pipelines.md) | Your command has many fixed options, several steps, or shell operators. |
| [Dynamic environment variables](guides/dynamic-environment-variables.md) | A value depends on the worktree or artifact directory and cannot be written in a static file. |
| [Planned configuration inputs](guides/planned-configuration-inputs.md) | Your program is driven by linked JSON, YAML, or INI files rather than flags. |
| [Matrices and mapping](guides/matrices-and-mapping.md) | You sweep parameters and need to know which directory holds which combination. |
| [Non-Git directory sources](guides/non-git-sources.md) | Your source is a plain directory rather than a Git repository. |
| [Workers and recovery](guides/workers-and-recovery.md) | You consume planned work in parallel, or need to recover an interrupted run. |

## Reference

Exact behavior, for when you need the rule rather than the recipe.

| Page | Contents |
| --- | --- |
| [CLI](reference/cli.md) | Every subcommand, its options, and its exit codes. |
| [Experiment layout](reference/experiment-layout.md) | Files inside an experiment directory and what writes each one. |
| [Source modes](reference/source-modes.md) | The four source modes and the rules each enforces. |
| [Reproducibility boundary](reference/reproducibility-boundary.md) | What RunForge captures, and what remains your responsibility. |

## Architecture

Design rationale, for changing RunForge rather than using it.

| Page | Contents |
| --- | --- |
| [Architecture and workflows](architecture/architecture-and-workflows.md) | Package responsibilities and how planning and execution fit together. |
| [Worker lifecycle and claims](architecture/worker-lifecycle-and-claims.md) | How ownership is decided, and why the worker is deliberately not a scheduler. |

## Operations

Deployment concerns for running RunForge on shared infrastructure.

| Page | Contents |
| --- | --- |
| [Shared filesystems](operations/shared-filesystems.md) | What multi-worker execution requires of your storage. |
| [Slurm and containers](operations/slurm-and-containers.md) | Recording an identity an operator can actually check. |
| [Troubleshooting](operations/troubleshooting.md) | Symptoms, causes, and the supported recovery for each. |
