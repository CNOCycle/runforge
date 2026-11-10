# Interruptible Machines

An interruptible machine, such as a discounted rental node, costs much less
than a dedicated one and can be stopped at any time. This page sets one up so
that you publish ablation plans once, the machine processes them without
further commands, an interruption costs little, and the results end up on your
own computer.

It assumes **one machine**, **a persistent volume** that survives a stop and a
start (`$WORKSPACE` below, `/workspace` in the demo), and a **resumable
program**: RunForge restarts the recorded command, it does not resume the
process. If the provider deletes the
volume or the instance, only data already copied home survives. The restart
mechanism and the two warnings are in
[Workers and recovery](../guides/workers-and-recovery.md#interruptible-machines);
the command is in the [CLI reference](../reference/cli.md#onstart).

This page covers:

- the workflow end to end, and which machine each step is on;
- the general steps on the rental machine, which every solution needs;
- the pull solution: your computer copies the results home in a loop;
- the push solution: the machine copies the results home to a receiver on your
  computer;
- a reference for the settings of `onstart.sh` and `transfer.sh`;
- two proposals that are not implemented: stopping the machine when the work is
  done, and fixes for slow links.

It does not cover installing your project or the provider's own setup, because
they depend on your project and your provider.

## What Has And Has Not Been Run

Read this before you rely on the page. It uses three labels.

- **Run:** the restart of interrupted work by `runforge onstart` (see the
  [CLI reference](../reference/cli.md#onstart), which has automated tests), and
  the ready-flag guard and the `--transfer-only` loop of `onstart.sh`, run
  locally with a stand-in transfer script.
- **Untested:** instructions written from the scripts and never run end to end.
  These are the `rsync` and `ssh` transfer in `transfer.sh`, host-key recording,
  key placement, the push loop, the pull loop on a real machine, and the two
  containers (pull and receiver). Try them with a small report root first.
- **Proposal:** not implemented. These are the optional stop section and the
  fixes in the last section.

So the unattended restart is the part that has been exercised. The
result-transfer workflow is a template you must test.

## The Workflow

Do these in order. Each step says which machine it is on, so you finish one
machine before you move to the other.

| Step | Machine | What |
| --- | --- | --- |
| 1 | rental | Install RunForge and your project's dependencies (project dependent) |
| 2 | rental | Deploy your project or repository (project dependent) |
| 3 | rental | Publish the plans |
| 4 | rental | Create the ready flag |
| 5 | rental | Set up `onstart.sh`: copy it, register it with the provider's on-start hook, and run it once |
| 6 | your computer | Configure the transfer: the pull loop ([Part 2](#part-2-your-computer-pull-solution), pull) or the receiver ([Part 3](#part-3-your-computer-push-solution), push) |
| 7 | automatic | When the machine is stopped and started again, the hook runs `onstart.sh`, and `runforge onstart` restarts what was cut off and processes the rest |
| 8 | automatic | The results are copied home, by the pull loop (pull) or by the machine (push) |

Steps 1 to 5 are [Part 1](#part-1-general-steps-on-the-rental-machine), which
the pull and the push solution both need. Step 6 is
[Part 2](#part-2-your-computer-pull-solution) for the pull solution, or
[Part 3](#part-3-your-computer-push-solution) for the push solution. Your
computer waits for the machine, so the machine comes first.

## The Files

Two files in [contrib/interruptible](../../contrib/interruptible/) do this.
They are examples to copy and adapt, and are not installed with RunForge.

| File | Runs on | Role |
| --- | --- | --- |
| [`onstart.sh`](../../contrib/interruptible/onstart.sh) | the rental machine, at every start; your computer with `--transfer-only` | ready-flag guard, then `runforge onstart`; or only the transfer |
| [`transfer.sh`](../../contrib/interruptible/transfer.sh) | your computer (pull) or the machine (push) | copies the report root |

## Choosing How Results Come Home

Transfer depends on the provider and your network, so RunForge does not
standardize it. There are two solutions, and **you pick one**:

| | Pull solution | Push solution |
| --- | --- | --- |
| Use it when | Your computer can reach the machine unattended, directly or through the ssh proxy and key you already use | Your computer cannot reach the machine unattended (a VPN or NAT, or an address you cannot look up), but the machine can reach your computer |
| Who copies | Your computer, by the loop in `onstart.sh --transfer-only` | The machine, started by `onstart.sh` |
| Secret on the machine | None | A restricted private key |
| Data leaves | Only while your computer is on and the machine is running | Every 15 minutes and at the end, even if you are away |
| Machine address | Must be looked up, it usually changes after a restart | Not needed, the destination is fixed |
| Loss if the machine is destroyed | Everything since the last successful pull | Everything since the last push |

Start with the pull solution. Use the push solution only when your computer
cannot reach the machine unattended. Both need you to log in to the machine once
for Part 1; they differ in the connection that must keep working afterwards.

## Part 1: General Steps On The Rental Machine

Everything here happens on the machine, over `ssh`, and every solution needs it.
Run it as one snippet, or step by step. Set the three paths once at the top; the
paths below are a demo, so use your own. The settings are also written to a file
so that the provider's on-start hook, which has none of your shell's
environment, uses the same values.

Steps 1 to 3 depend on your project, and this page does not cover them. The
lines shown are placeholders to replace.

```bash
# Settings: every step below uses them.
export WORKSPACE=/workspace                 # the persistent volume
export REPORT_ROOT="$WORKSPACE/reports"     # where the plans are published
export SOURCE="$WORKSPACE/project"          # your deployed project

# Step 1 (project dependent): install RunForge and your project's dependencies.
#   RunForge: see the README, Install. Install rsync too if the image lacks it.
#   Put them on the volume or in the image so they survive a stop.

# Step 2 (project dependent): deploy your project or repository to "$SOURCE".
#   A git clone, a copy, or a prebuilt image.

# Step 3 (project dependent): publish the plans into "$REPORT_ROOT". Replace this
#   example with the plan or matrix of your project.
runforge plan --name ablation --matrix matrix.json --source-mode directory-snapshot --source-path "$SOURCE" --out-dir "$REPORT_ROOT" -- python train.py --lr '{LR}' --seed '{SEED}'

# Step 4: when every plan is published, create the ready flag. onstart.sh does
#   nothing until it exists, so a start in the middle of publication starts nothing.
touch "$WORKSPACE/.ready_to_run"

# Step 5: set up onstart.sh (copy it from contrib/interruptible first).
cp onstart.sh "$WORKSPACE/onstart.sh" && chmod +x "$WORKSPACE/onstart.sh"
cat > "$WORKSPACE/onstart.env" <<ENV
WORKSPACE=$WORKSPACE
REPORT_ROOT=$REPORT_ROOT
ENV
#   Register this command with the provider's on-start hook, so it runs at every start:
#     ONSTART_ENV=$WORKSPACE/onstart.env $WORKSPACE/onstart.sh
#   The hook only runs at the next start, so run it once now by hand:
ONSTART_ENV="$WORKSPACE/onstart.env" "$WORKSPACE/onstart.sh"
```

Plans record absolute paths, so publish them on the machine, into the report
root, and do not copy a report root from another path
([Shared filesystems](shared-filesystems.md)). For a matrix of ablations see
[Matrices and mapping](../guides/matrices-and-mapping.md).

Notes:

- The script returns at once. Everything it starts writes to `LOG` (default
  `$WORKSPACE/onstart.log`); each experiment keeps its own logs. The settings
  (`WORKSPACE`, `REPORT_ROOT`, `READY_FLAG`, `LOG`) are environment variables, or
  a file named by `ONSTART_ENV`; the header of the script lists them.
- Leave `TRANSFER_SCRIPT` unset in the pull solution: the machine copies
  nothing. The push solution sets it in [Part 3](#part-3-your-computer-push-solution).
- Running it twice does no harm: a second `runforge onstart` on the same report
  root exits with code 2 while the first is alive.
- Use exactly one `onstart` entry point per machine. `--single-node` confirms
  that this machine has just started and nothing else works on the report root.

The machine now works through the queue. Go to your computer.

## Part 2: Your Computer, Pull Solution

Everything here happens on your computer. You can already reach the machine, so
your public key is registered there, and you may already use an ssh proxy. What
remains is configuration. You need `ssh` and `rsync` installed.

1. Make a working directory and copy the files into it:

   ```bash
   mkdir -p ~/rental && cp contrib/interruptible/onstart.sh contrib/interruptible/transfer.sh ~/rental/
   chmod +x ~/rental/onstart.sh ~/rental/transfer.sh
   ```

2. Fill in the top of `~/rental/transfer.sh`:

   | Setting | Value |
   | --- | --- |
   | `PULL_KEY` | the private key you already use for the machine |
   | `REMOTE_ROOT` | the report root on the machine, for example `/workspace/reports` |
   | `SSH_EXTRA` | your proxy, for example `-o ProxyJump=bastion`; empty if none |
   | `KNOWN_HOSTS` | a file that holds the machine's host key (next step) |
   | `current_endpoint` | a command that prints `user@host port` for the machine as it is now, and fails when it is stopped (your provider's CLI, or a line from your ssh config) |

3. Store the machine's host key under a fixed name. The address changes after a
   restart, so `transfer.sh` looks the key up under `rental-host`
   (`HOST_KEY_ALIAS`) and keeps `StrictHostKeyChecking=yes`. `current_endpoint` prints
   `user@host port`; `HOST` below is only the hostname or IP, without `user@`,
   and `PORT` is the port. Check the fingerprint against the one your provider
   shows:

   ```bash
   HOST=machine.example.org    # hostname or IP only, without user@
   PORT=2222                   # the ssh port
   ssh-keyscan -p "$PORT" "$HOST" | sed 's/^[^ ]* /rental-host /' > ~/rental/known_hosts
   ssh-keygen -lf ~/rental/known_hosts
   ```

4. Run one pull by hand while the machine is up, and look at the result:

   ```bash
   ~/rental/onstart.sh --transfer-only "$HOME/rental-reports"; echo "exit $?"
   ```

   It must exit 0. If `current_endpoint` fails because the machine is stopped,
   it also exits 0 and copies nothing. Only `claim/` and `.workspace/` are
   skipped, and nothing is ever deleted in `~/rental-reports`. This transfer is
   **untested** end to end, so run it on a small report root first.

5. Start the pull loop. It is the same loop `onstart.sh` runs on the machine for
   the push solution, here pulling every 900 seconds. It keeps going when a
   pull fails, and logs the failure:

   ```bash
   nohup setsid ~/rental/onstart.sh --transfer-only "$HOME/rental-reports" --loop 900 >> ~/rental/pull.log 2>&1 < /dev/null &
   ```

   A lock in `transfer.sh` stops two pulls overlapping. The loop ends when you
   stop it or when your computer restarts, so start it again after a reboot
   (from your login script, for example), or run it in a container as below.
   Without `--loop` the command pulls once and exits with the exit code of
   `transfer.sh`.

**Running it in a tiny container.** The container's process is the loop. This is
**untested**. Use a small image with
`bash`, `rsync` and an ssh client (for example Alpine with
`apk add bash rsync openssh-client util-linux`), mount `~/rental`, the key,
and `~/rental-reports`, and run
`/rental/onstart.sh --transfer-only /reports --loop 900` as the command.
Start it with `docker run -d --restart unless-stopped`, so it comes back after a
reboot. The proxy host and the key must be readable inside the container, and
`KNOWN_HOSTS` must point at the mounted file.

## Part 3: Your Computer, Push Solution

Use this instead of Part 2 when your computer cannot reach the machine
unattended but the machine can reach your computer, for example when the machine
is behind a VPN. The machine issues the transfer, so it needs a key and a place
to send to. This whole part is **untested** end to end.

Push still needs access to the machine from your computer **once**. Part 1
already required it (a provider proxy, a console or a key), and step 5 below
uses it to place the key and the settings. After that, only a connection from
the machine to your computer has to work. If you have no way to log in from
your computer at all, place the three files and the setting of step 5 through
the provider's console or file upload instead.

Do Part 1 first. Everything here happens on your computer, and the last step
configures the machine over `ssh`, so you do not switch machines.

**Which key is passed in advance?** A dedicated key pair that you create on your
own computer before you enable the first push. Its public half goes into the
receiver, limited to copying into one directory with no shell. Its private half
is placed once on the machine's persistent volume, together with the receiver's
host key. Nothing then has to be authorized after a boot. A key generated on the
machine at each start cannot work unattended, because its public half would have
to be added at home before the first push. Treat the private half as exposed
whenever the machine is: it can only write into the receiver's directory, and
you revoke it by removing one line at home.

1. Make a working directory and create the key pair:

   ```bash
   mkdir -p ~/rental && cd ~/rental
   cp /path/to/contrib/interruptible/transfer.sh . && chmod +x transfer.sh
   ssh-keygen -t ed25519 -N "" -C rental-push -f ./rental_push_key
   ssh-keygen -t ed25519 -N "" -f ./ssh_host_key
   ```

   The second key is the receiver's host key, made here so that it is known in
   advance and stays the same when the container is re-created.

2. Run a receiving container on your computer. This is an **untested**
   example. It has an ssh server, one user `receiver`, password login
   off, and one mounted directory for the files. The `authorized_keys` line forces
   `rrsync`, which confines `rsync` to that directory. Put the public half from
   step 1 in it:

   ```bash
   echo "restrict,command=\"/usr/bin/rrsync /data\" $(cat rental_push_key.pub)" > authorized_keys
   ```

   `Dockerfile` (Debian ships `rrsync` with `rsync`; check its path in your
   image) and `receiver.conf`:

   ```text
   FROM debian:stable-slim
   RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server rsync \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/sh receiver && passwd -l receiver \
    && mkdir -p /run/sshd /data && chown receiver /data
   COPY receiver.conf /etc/ssh/sshd_config.d/receiver.conf
   EXPOSE 22
   CMD ["/usr/sbin/sshd", "-D", "-e"]
   ```

   ```text
   PasswordAuthentication no
   PermitRootLogin no
   AllowUsers receiver
   HostKey /etc/ssh/keys/ssh_host_key
   ```

   ```bash
   docker build -t rental-receiver .
   docker run -d --name rental-receiver -p 2222:22 \
       -v "$HOME/rental-reports:/data" \
       -v "$PWD/authorized_keys:/home/receiver/.ssh/authorized_keys:ro" \
       -v "$PWD/ssh_host_key:/etc/ssh/keys/ssh_host_key:ro" \
       rental-receiver
   ```

   The machine must be able to reach the published port: an address on the VPN,
   or a forwarded port on your router. Call that address `$RECEIVER`.

3. Store the receiver's host key under the fixed name `rental-host`, and check its
   fingerprint against the key you made:

   ```bash
   ssh-keyscan -p 2222 "$RECEIVER" | sed 's/^[^ ]* /rental-host /' > known_hosts
   ssh-keygen -lf known_hosts; ssh-keygen -lf ssh_host_key.pub
   ```

4. Fill in the push settings at the top of `~/rental/transfer.sh`:

   | Setting | Value |
   | --- | --- |
   | `PUSH_DEST` | `receiver@ADDRESS:/` (`rrsync` makes `/` the receiving directory) |
   | `PUSH_PORT` | the published port, `2222` above |
   | `PUSH_KEY` | where the private key will be on the machine (default `$WORKSPACE/.push_key`) |
   | `KNOWN_HOSTS` | where the host key will be on the machine (default `$WORKSPACE/.known_hosts`) |

5. Send the three files to the machine, tell `onstart.sh` to push, and run it
   again. This uses the same `ssh` access, and the proxy if any, as the pull
   solution. `MACHINE` and `PORT` are how you reach it; `W` is its `$WORKSPACE`
   from Part 1:

   ```bash
   W=/workspace
   scp -P "$PORT" rental_push_key "$MACHINE:$W/.push_key"
   scp -P "$PORT" known_hosts "$MACHINE:$W/.known_hosts"
   scp -P "$PORT" transfer.sh "$MACHINE:$W/transfer.sh"
   ssh -p "$PORT" "$MACHINE" "chmod 600 $W/.push_key && echo TRANSFER_SCRIPT=$W/transfer.sh >> $W/onstart.env && ONSTART_ENV=$W/onstart.env $W/onstart.sh"
   ```

   The script, when `TRANSFER_SCRIPT` is set, pushes every 15 minutes
   (`TRANSFER_INTERVAL`) and once more when `runforge onstart` finishes. Running
   it again while the first run is alive is harmless: its second
   `runforge onstart` exits with code 2, and the log says so. From the next start
   on, the hook runs it with the push enabled.

## Limits

- One machine only. Do not run `onstart` on a machine that shares the report
  root with others.
- The program must be resumable; RunForge restarts the command and restores the
  artifacts, nothing more.
- Directories named `claim` or `.workspace` anywhere under the report root are
  excluded from transfer, including any your own program writes.
- Instances that are destroyed rather than stopped are out of scope: the copy at
  home is then the only survivor.
- What is untested is listed in [What Has And Has Not Been Run](#what-has-and-has-not-been-run).

## Reference: Configuring onstart.sh And transfer.sh

The values here are the ones in the scripts. Each script's header says the same.
Reference only: the procedure is in Parts 1 to 3.

### onstart.sh

```text
onstart.sh                                          on the rental machine
onstart.sh --transfer-only LOCAL_DEST [--loop SECONDS]    on your computer (pull solution)
```

Without arguments it checks the ready flag and starts `runforge onstart`, and it
returns at once. With `--transfer-only` it skips both, runs
`transfer.sh pull LOCAL_DEST` in the foreground, and either exits with the
transfer's exit code (once) or repeats every `SECONDS` (with `--loop`). An
unknown flag, or `--transfer-only` without a destination, prints the usage and
exits 2.

Settings are environment variables, or a file of `NAME=value` lines named by
`ONSTART_ENV`, which is how the provider's on-start hook gets them. The script
reads the file after the environment, so a value in the file wins. Part 1 writes
this file (the pull solution):

```text
WORKSPACE=/workspace
REPORT_ROOT=/workspace/reports
```

Step 5 of Part 3 adds one line for the push solution only:

```text
TRANSFER_SCRIPT=/workspace/transfer.sh
```

| Setting | Default | Used by | Meaning |
| --- | --- | --- | --- |
| `ONSTART_ENV` | none | both | the settings file to read |
| `WORKSPACE` | `/workspace` | rental machine | the persistent volume |
| `REPORT_ROOT` | `$WORKSPACE/reports` | rental machine | where the plans are published, and what `runforge onstart` processes |
| `READY_FLAG` | `$WORKSPACE/.ready_to_run` | rental machine | the script does nothing until this file exists |
| `LOG` | `$WORKSPACE/onstart.log` | rental machine | output of everything the script starts |
| `TRANSFER_SCRIPT` | none (rental machine); `transfer.sh` beside `onstart.sh` (`--transfer-only`) | both | the transfer script. When set on the machine, it pushes (push solution) |
| `TRANSFER_INTERVAL` | `900` | rental machine | seconds between pushes |

### transfer.sh

```text
transfer.sh pull LOCAL_DEST     on your computer
transfer.sh push REPORT_ROOT    on the rental machine
```

Anything else prints the usage and exits 2. The contract, for both modes:

- Exit 0 on success. Any other exit is logged and retried at the next run. It
  also exits 0, quietly, when another copy holds the lock. A failed `rsync` exits
  with its own non-zero code.
- Safe to run repeatedly and concurrently, and to be killed at any point
  (`rsync --partial` resumes).
- Copies the report root without `claim/` and `.workspace/`, which describe a
  worker that exists only on the machine, and never deletes at the destination.
- A stopped machine is normal: when `current_endpoint` reports it by failing,
  `pull` exits 0 quietly. A failed connection that it did not report is a
  non-zero exit, which the caller logs and retries.

The settings are the lines in the block at the top of the file, which you edit.
Machine-side paths default from `WORKSPACE`.

| Setting | Default | Solution | Meaning |
| --- | --- | --- | --- |
| `PUSH_DEST` | `backup@home.example.org:/home/backup/reports/` | push | where to send. `receiver@ADDRESS:/` for the receiver container |
| `PUSH_PORT` | `22` | push | ssh port of the receiver |
| `PUSH_KEY` | `$WORKSPACE/.push_key` | push | the dedicated private key, on the machine |
| `KNOWN_HOSTS` | `$WORKSPACE/.known_hosts` | both | the file holding the other side's host key, stored under `HOST_KEY_ALIAS` |
| `HOST_KEY_ALIAS` | `rental-host` | both | the fixed name the host key is stored under, so a changed address still matches |
| `REMOTE_ROOT` | `/workspace/reports` | pull | the report root on the machine |
| `PULL_KEY` | `$HOME/.ssh/rental_pull_key` | pull | the private key you already use for the machine |
| `SSH_EXTRA` | empty | both | extra ssh options, for example `-o ProxyJump=bastion` |
| `current_endpoint` | a placeholder command | pull | a function that prints `user@host port` for the machine as it is now, and fails when it is stopped |

`rsync` runs as `rsync -a --partial` with `claim/` and `.workspace/` excluded and
never `--delete`. `ssh` runs with `BatchMode=yes` and
`StrictHostKeyChecking=yes`. These two lines are in `rsync_copy` and `ssh_with`
in the script, and are not settings.

## [Optional] Stop The Machine When Everything Is Done

This is a **proposal** and not implemented: nothing in `contrib/interruptible`
stops a machine. A rental node bills until it is stopped or destroyed, and how to
do either depends on the provider.

- **Detect completion.** `runforge discover "$REPORT_ROOT"` ends with a summary
  of counts per status. The work is finished when `created`, `init` and
  `inprogress` are all 0. `failed` and `invalid` experiments are finished too,
  but they need a decision from you: decide beforehand whether the machine
  stops with them present or stays up.
- **Pull solution: decide at home.** Extend the pull loop to do this, from your
  computer, so the provider credential stays home:
  1. After a pull that really copied the data, run `runforge discover` on the
     local copy. It only reads the recorded status, so it works although the
     copy's path differs from the machine's (checked on a copy made with the
     same excludes as `transfer.sh`; only the printed paths differ). Treat the
     work as finished when `created`, `init` and `inprogress` are all 0.
  2. When the copy first shows "finished", do not stop yet. Run one more pull
     that starts after that moment, runs `rsync` and succeeds.
  3. Then run your provider's stop command.

  Step 2 is needed because a finished status does not show that the result files
  are complete: files copied while the program was still writing them can be
  partial or from different moments (see the last section). A copy that shows
  `inprogress` may already be finished on the machine, and the next pull shows
  it.
- **Push solution: decide on the machine.** Extend `onstart.sh` so that, after
  `runforge onstart` returns, it runs a final push, and stops the machine only
  after that push actually transfers the data and succeeds. This needs a provider
  credential on the machine, so use one scoped to this instance.
- **Do not trust a plain exit 0.** A transfer that was skipped, because another
  holds the lock or the machine is not reported, also exits 0. Read
  [Known Issue](#known-issue-slow-links-and-overlapping-transfers) first: a stop
  needs a transfer that is known to have run.
- **Stop, do not destroy, at first.** A stopped machine keeps its volume, so a
  wrong status check costs a restart and not the data. If it starts again,
  `runforge onstart` finds everything finished and returns. Destroy only after
  you have checked the copy at home.

## Known Issue: Slow Links And Overlapping Transfers

On a slow link a transfer can still be running when the next one is due. This
section says what the scripts do now, what can go wrong, and what is proposed.
The proposals are **not implemented**: `transfer.sh` and `onstart.sh` do not
contain them yet.

### What happens now

- `transfer.sh pull` and `transfer.sh push` each take a lock. If a transfer
  holds it, the next run exits 0 without copying, and the first transfer keeps
  going. The next transfer is skipped, not queued.
- The loops (`onstart.sh` on the machine, and `--transfer-only --loop`) wait for
  a transfer to end before they sleep. A slow transfer therefore makes the
  interval longer, and transfers never pile up.
- After a transfer ends, the next run copies whatever changed. `rsync --partial`
  continues an interrupted large file.
- If a transfer is killed, for example when the machine stops, the lock is
  released with the process, and the next run resumes the partial file.

### What can go wrong

1. **A stalled transfer holds the lock.** `transfer.sh` sets no timeout and no
   keepalive. When the machine is cut off abruptly, the other side's `rsync` can
   wait on a dead connection, and every later transfer then skips because the
   lock is held.
2. **The final push can be skipped.** `onstart.sh` pushes once more when
   `runforge onstart` ends. If the push loop holds the lock at that moment, the
   final push exits 0 without copying, and the last results wait for the next
   loop run. A zero exit therefore does not prove that the data is home, which
   matters if you stop the machine after a push.
3. **There is no bandwidth control.** A long transfer can compete with your other
   traffic, and `transfer.sh` has no `--bwlimit` or `--compress` option.
4. **A file rewritten during a transfer can be copied half-written.** A
   checkpoint that your program rewrites in place can arrive inconsistent. The
   next transfer copies it again, so the home copy heals, but it can be wrong
   for a while.

### Proposed solutions

1. Add `--timeout=300` to `rsync`, and `-o ServerAliveInterval=30`,
   `-o ServerAliveCountMax=4` and `-o ConnectTimeout=20` to the `ssh` options in
   `transfer.sh`. A stalled transfer then ends by itself and releases the lock.
2. Make the final push wait for the lock, with a limit, instead of skipping. Also
   give `transfer.sh` a distinct exit code for "skipped", so that a caller that
   needs the data to be home (for example one that stops the machine) can tell a
   transfer that ran from one that did not.
3. Add an optional `RSYNC_EXTRA` setting that is appended to the `rsync` command,
   for example `--bwlimit=2000` (KB/s) or `--compress`.
4. Write checkpoints atomically in your program: write to a temporary file in the
   same directory, then rename it. A transfer then sees either the old file or
   the new one. This is the program's responsibility, not RunForge's.

Until 1 and 2 are done, treat a skipped transfer as normal, check the log for a
transfer that has not ended for much longer than usual, and do not stop the
machine on the strength of one exit code 0.
