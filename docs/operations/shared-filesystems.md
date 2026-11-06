# Shared Filesystems

Running several workers against one report root requires the storage underneath
to provide two things. This page states them plainly, because RunForge cannot
verify them for you.

## Supported Scenarios

RunForge supports local, single-machine use, and clusters whose workers share
one filesystem. Backing up an experiment is fine. Running a moved or copied experiment
or report root from a different path is not supported and can break published
plans, as described below.

## The Two Requirements

**Every worker must see the same experiment directories and metadata.** A worker
that cannot see a claim will create its own.

**Exclusive directory creation must be atomic and coherent.** Claims are
acquired by creating a directory that must fail for every process but one. If
that operation is not atomic across hosts, or if the result is cached rather
than coherent, two workers can both believe they own the same experiment.

When planning and execution use different machines, mount the shared report
root at the same absolute path on the planning machine and every worker. Plans
can contain `{ARTIFACT_DIR}` and `{INPUT_DIR}` paths resolved at planning time.
Mounting the same files at a different path, or moving them, does not update
those recorded paths, and a command can then fail or use the old location. Local
single-machine execution needs no network filesystem.

Workers running Git-backed plans must reach the recorded repository. A
`verified-directory` plan records an absolute directory that must exist at the
same path on every worker. A `directory-snapshot` plan carries its source in the
experiment directory, so the original source directory can be absent.

## Worker Workdir Placement

A worker builds each Git or directory-snapshot workdir at `.workspace/` inside
the experiment directory, so it lives on the shared report storage and is
visible from every host. Only the worker holding the experiment's claim builds,
deletes, or replaces it. Budget report storage for one source copy per running
experiment, and expect checkout and copy speed to follow that filesystem;
there is no option to place workdirs on separate local scratch.

## What Is And Is Not Proven

A local POSIX filesystem is the reference environment, and the cross-process
contention test exercises it directly: two processes race for one plan and
exactly one executes.

Network filesystems are **not** covered by that test. NFS and SMB can be
configured in ways that satisfy the requirements above and in ways that do not,
and the difference is in mount options and server behavior rather than anything
RunForge can inspect. Verify it for your deployment before trusting parallel
workers across hosts.

The practical check is the one the test performs: plan a small matrix on the
shared root, run several workers against it from the machines you intend to use,
and confirm each experiment ran exactly once and no claims were left behind.

## Containers On One Host

Containers sharing a bind-mounted report root are using one local filesystem.
That is the reference environment, and the requirements are met.

## When The Guarantees Are Unavailable

Use one worker. A single worker needs no cross-host coordination, and
`--max-tasks` still bounds how much it does per invocation.
