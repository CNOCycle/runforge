# Shared Filesystems

Running several workers against one report root requires the storage underneath
to provide two things. This page states them plainly, because RunForge cannot
verify them for you.

## The Two Requirements

**Every worker must see the same experiment directories and metadata.** A worker
that cannot see a claim will create its own.

**Exclusive directory creation must be atomic and coherent.** Claims are
acquired by creating a directory that must fail for every process but one. If
that operation is not atomic across hosts, or if the result is cached rather
than coherent, two workers can both believe they own the same experiment.

Workers must also be able to reach the recorded source path. A Git-backed plan
records a repository location; a `verified-directory` plan records an absolute
directory that must exist at the same path on every worker.

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
