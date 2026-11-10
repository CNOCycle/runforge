#!/usr/bin/env bash
# The transfer interface: a template to copy, fill in for your provider and
# network, and point TRANSFER_SCRIPT (see onstart.sh) at it. The rsync and
# ssh parts have not been tested end to end; read them before relying on them.
#
#   transfer.sh push REPORT_ROOT   run on the rental machine (onstart.sh calls it)
#   transfer.sh pull LOCAL_DEST    run on your computer (onstart.sh --transfer-only calls it)
#
# Contract, for push and pull alike:
#   - exit 0 on success; any other exit is logged and retried at the next run;
#   - safe to run repeatedly, concurrently, and to be killed at any point
#     (rsync --partial resumes; the lock below stops overlapping runs);
#   - copy the report root without claim/ and .workspace/, which describe a
#     worker that only exists on the machine, and never delete at the destination;
#   - a stopped machine is normal: when current_endpoint reports it by failing,
#     exit 0 quietly; a failed connection it did not report exits non-zero.
set -u

# ---- fill these in ---------------------------------------------------------
PUSH_DEST="backup@home.example.org:/home/backup/reports/"   # push: where to send
PUSH_PORT=22                                                # push: ssh port of the receiver
PUSH_KEY=${WORKSPACE:-/workspace}/.push_key                # push: dedicated key
KNOWN_HOSTS=${WORKSPACE:-/workspace}/.known_hosts          # pinned host key, stored as HOST_KEY_ALIAS
HOST_KEY_ALIAS=rental-host                                  # fixed name, so a changed address still matches
REMOTE_ROOT=/workspace/reports                              # pull: report root on the machine
PULL_KEY=$HOME/.ssh/rental_pull_key                         # pull: dedicated key
SSH_EXTRA=""   # extra ssh options, e.g. "-o ProxyJump=bastion" for your existing proxy
# pull: print "user@host port" for the machine as it is now, or fail if it is stopped
current_endpoint() { your-provider-command; }
# ----------------------------------------------------------------------------

rsync_copy() {   # rsync_copy SSH_COMMAND SOURCE DESTINATION
    rsync -a --partial --exclude=claim/ --exclude=.workspace/ -e "$1" "$2" "$3"
}
ssh_with() {     # ssh_with PORT KEY
    echo "ssh -p $1 -i $2 -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o HostKeyAlias=$HOST_KEY_ALIAS $SSH_EXTRA"
}

case ${1:-} in
    push)
        root=${2:?usage: transfer.sh push REPORT_ROOT}
        exec 9>"${root%/}.push.lock"; flock -n 9 || exit 0
        rsync_copy "$(ssh_with "$PUSH_PORT" "$PUSH_KEY")" "${root%/}/" "$PUSH_DEST"
        ;;
    pull)
        dest=${2:?usage: transfer.sh pull LOCAL_DEST}
        endpoint=$(current_endpoint) || exit 0
        read -r target port <<<"$endpoint"
        mkdir -p "$dest"
        exec 9>"${dest%/}.pull.lock"; flock -n 9 || exit 0
        rsync_copy "$(ssh_with "${port:-22}" "$PULL_KEY")" "$target:$REMOTE_ROOT/" "${dest%/}/"
        ;;
    *)
        echo "usage: transfer.sh push REPORT_ROOT | pull LOCAL_DEST" >&2
        exit 2
        ;;
esac
