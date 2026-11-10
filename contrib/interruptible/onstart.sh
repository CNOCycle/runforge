#!/usr/bin/env bash
# On-start script for an interruptible machine: an example to copy and adapt.
#
# Register it with the provider's on-start hook so it runs every time the
# machine starts. After publishing the plans for the first time, run it once by
# hand. It returns at once; in the background it
#   1. runs `runforge onstart --single-node REPORT_ROOT`, then one transfer;
#   2. when TRANSFER_SCRIPT is set, runs `TRANSFER_SCRIPT push REPORT_ROOT`
#      every TRANSFER_INTERVAL seconds (see transfer.sh).
#
# The same file runs on your computer to pull results home (pull solution):
#   onstart.sh --transfer-only LOCAL_DEST [--loop SECONDS]
# It then skips the ready flag and runforge, runs `transfer.sh pull LOCAL_DEST`
# in the foreground once, or every SECONDS with --loop (keep it running with
# nohup setsid, or as a container's process). TRANSFER_SCRIPT defaults to
# transfer.sh beside it.
#
# Settings are environment variables, or a file of assignments named by
# ONSTART_ENV.
#   WORKSPACE          persistent volume                 default /workspace
#   REPORT_ROOT        where the plans are published     default $WORKSPACE/reports
#   READY_FLAG         create it after publishing plans  default $WORKSPACE/.ready_to_run
#   LOG                output of everything below        default $WORKSPACE/onstart.log
#   TRANSFER_SCRIPT    optional transfer script          default none
#   TRANSFER_INTERVAL  seconds between transfers         default 900
set -u

if [ -n "${ONSTART_ENV:-}" ] && [ -f "$ONSTART_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ONSTART_ENV"
    set +a
fi
WORKSPACE=${WORKSPACE:-/workspace}
REPORT_ROOT=${REPORT_ROOT:-$WORKSPACE/reports}
READY_FLAG=${READY_FLAG:-$WORKSPACE/.ready_to_run}
LOG=${LOG:-$WORKSPACE/onstart.log}
TRANSFER_SCRIPT=${TRANSFER_SCRIPT:-}
TRANSFER_INTERVAL=${TRANSFER_INTERVAL:-900}

TRANSFER_ONLY=0
LOOP=""
DEST=""
while [ $# -gt 0 ]; do
    case $1 in
        --transfer-only) TRANSFER_ONLY=1 ;;
        --loop) LOOP=${2:?usage: --loop SECONDS}; shift ;;
        -*) echo "usage: onstart.sh [--transfer-only LOCAL_DEST [--loop SECONDS]]" >&2; exit 2 ;;
        *) DEST=$1 ;;
    esac
    shift
done

if [ "$TRANSFER_ONLY" = 1 ]; then
    [ -n "$DEST" ] || { echo "usage: onstart.sh --transfer-only LOCAL_DEST [--loop SECONDS]" >&2; exit 2; }
    TRANSFER_SCRIPT=${TRANSFER_SCRIPT:-$(dirname "$0")/transfer.sh}
    while true; do
        "$TRANSFER_SCRIPT" pull "$DEST"; status=$?
        [ -n "$LOOP" ] || exit "$status"
        [ "$status" = 0 ] || echo "transfer failed with exit $status" >&2
        sleep "$LOOP"
    done
fi

if [ ! -f "$READY_FLAG" ]; then
    echo "not ready: $READY_FLAG does not exist" >&2
    exit 0
fi

# runforge onstart refuses a second copy on the same report root (exit 2).
nohup setsid bash -c '
    runforge onstart --single-node "$2"
    echo "runforge onstart exited with $?"
    if [ -n "$1" ]; then
        "$1" push "$2" || echo "transfer failed with exit $?"
    fi
' _ "$TRANSFER_SCRIPT" "$REPORT_ROOT" >>"$LOG" 2>&1 </dev/null &

if [ -n "$TRANSFER_SCRIPT" ]; then
    # One loop per machine: flock refuses a loop started by a second run of this script.
    nohup setsid flock -n "$WORKSPACE/.transfer-loop.lock" bash -c '
        while true; do
            "$0" push "$1" || echo "transfer failed with exit $?"
            sleep "$2"
        done
    ' "$TRANSFER_SCRIPT" "$REPORT_ROOT" "$TRANSFER_INTERVAL" >>"$LOG" 2>&1 </dev/null &
fi
exit 0
