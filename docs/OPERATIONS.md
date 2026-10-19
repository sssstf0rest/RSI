# Raspberry Pi operation

This guide targets Raspberry Pi 5, 8 GB, Ubuntu Server 24.04 ARM64. The
installation commands and service units are provided for deployment; they have
not been executed on a Pi. Run each validation below before enabling services.

The Pi runs Python orchestration, SQLite, Git, and local evaluations. Official
Codex handles cloud model inference and saved ChatGPT authentication. One model
worker runs at a time. An idle worker service polls the local queue and makes no
model requests until a task is queued. Telegram long polling does make network
requests while idle.

## Install under a dedicated account

Use a clean published revision containing this implementation. If the remote
default branch does not contain it, check out the published implementation
branch or commit before installing. Commands below assume `/srv/rsi` is new.

```bash
sudo apt update
sudo apt install git python3-venv curl bubblewrap
sudo useradd --system --create-home --home-dir /var/lib/rsi --shell /bin/bash rsi
sudo install -d -o rsi -g rsi -m 0750 /srv/rsi
sudo -u rsi -H git clone https://github.com/sssstf0rest/RSI.git /srv/rsi
sudo -u rsi -H python3 -m venv /srv/rsi/.venv
sudo -u rsi -H /srv/rsi/.venv/bin/python -m pip install -e /srv/rsi
sudo -u rsi -H bash -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
```

The official installer and Linux ARM64 binary are documented in the
[Codex repository](https://github.com/openai/codex#installing-and-running-codex-cli).
Confirm the installer placed `codex` in `/var/lib/rsi/.local/bin`; if the chosen
installation uses another directory, update the PATH in both units. Record the
installed CLI version with the deployment rather than assuming an upstream
default will remain unchanged.

Log in as **the same account that runs the services**, with that account's HOME:

```bash
sudo -u rsi -H /var/lib/rsi/.local/bin/codex --version
sudo -u rsi -H /var/lib/rsi/.local/bin/codex login --device-auth
sudo -u rsi -H /var/lib/rsi/.local/bin/codex login status
sudo -u rsi -H env PATH=/srv/rsi/.venv/bin:/var/lib/rsi/.local/bin:/usr/local/bin:/usr/bin:/bin \
  /srv/rsi/.venv/bin/rsi --repo /srv/rsi doctor
```

Complete the device-code browser step yourself. Device login can require enabling
the option in ChatGPT security settings. Keep credential storage and refresh
inside official Codex; a login under your interactive Linux account does not
authenticate the `rsi` account. See the
[official authentication guide](https://learn.chatgpt.com/docs/auth#login-on-headless-devices).

## Validate the Linux sandbox

`rsi doctor` probes workspace and temporary-file writes inside the sandbox. It
does not call a model. The repository must also be clean and committed before a
development task starts.

Ubuntu 24.04 may require the targeted `bwrap-userns-restrict` AppArmor profile even
after `bubblewrap` is installed. Follow the current
[official Linux sandbox instructions](https://learn.chatgpt.com/docs/sandboxing#prerequisites)
to install and load the distribution's extra profile, then rerun `rsi doctor`.
This setup is source-checked but untested on the target Pi. Keep namespace
restrictions and the execution sandbox enabled; diagnose a failing prerequisite
before scheduling work. The provided service units do not restrict namespace
creation or turn off the sandbox.

Run the local test suite with the service interpreter:

```bash
sudo -u rsi -H bash -c 'cd /srv/rsi && .venv/bin/python -m unittest discover -s tests -v'
```

Before unattended use, perform one small live task and inspect its structured
record, patch, independent verification results, and failure classification.
That consumes plan usage. Local mock tests do not establish working OAuth
inference, ARM64 sandbox compatibility, or measured improvement on fresh tasks.

For example, submit a bounded task and consume one queued job after authentication
and sandbox checks succeed:

```bash
sudo -u rsi -H /srv/rsi/.venv/bin/rsi --repo /srv/rsi enqueue --task 'Add a small documented helper with regression tests.'
sudo -u rsi -H env PATH=/srv/rsi/.venv/bin:/var/lib/rsi/.local/bin:/usr/local/bin:/usr/bin:/bin \
  /srv/rsi/.venv/bin/rsi --repo /srv/rsi worker --max-jobs 1
sudo -u rsi -H /srv/rsi/.venv/bin/rsi --repo /srv/rsi jobs
```

## Configure the Telegram bot

Create the bot using BotFather and obtain your own numeric Telegram user ID.
Only messages from that user in their private chat are accepted. Group messages
and other users are ignored before reading run or version history.

Keep the bot token in a private file outside the checkout. The worker unit never
loads this file; only the Telegram unit receives these environment variables.

```bash
sudo install -d -o root -g rsi -m 0750 /etc/rsi
sudo install -o root -g rsi -m 0640 /dev/null /etc/rsi/telegram.env
sudoedit /etc/rsi/telegram.env
```

Enter actual values in the editor:

```text
TELEGRAM_BOT_TOKEN=replace_with_BotFather_token
TELEGRAM_ALLOWED_USER_ID=replace_with_your_positive_numeric_ID
```

Do not put tokens into task text, prompts, Git, or service command lines. Bot
exceptions omit Telegram URLs and API response bodies, and the known bot token
is removed from replies. A command containing the configured token is refused
before a task payload is stored. This is not a general secret detector for
arbitrary user-supplied text.

The interfaces use outbound HTTPS; long polling requires no inbound port or
public webhook. Bot/model connections are separate from candidate subprocesses,
whose configured execution network access remains disabled.

## Install and explicitly start services

The following commands activate the worker and Telegram services. They should be
run only after the preceding checks and configuration are complete.

```bash
sudo install -m 0644 /srv/rsi/deploy/rsi-worker.service /etc/systemd/system/rsi-worker.service
sudo install -m 0644 /srv/rsi/deploy/rsi-telegram.service /etc/systemd/system/rsi-telegram.service
sudo systemd-analyze verify /etc/systemd/system/rsi-worker.service /etc/systemd/system/rsi-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now rsi-worker.service rsi-telegram.service
sudo systemctl status rsi-worker.service rsi-telegram.service
```

Both services run as `rsi`, use a private umask, restart only after failure, and
stop their entire process control group. Network requests use finite timeouts.
`TimeoutStopSec=30` bounds shutdown, and systemd terminates remaining processes
after that interval. Repeated startup failures hit a finite restart limit; after
repair, use `systemctl reset-failed` before starting again.

```bash
sudo journalctl -u rsi-worker.service -u rsi-telegram.service --since today
sudo systemctl stop rsi-worker.service rsi-telegram.service
```

Stop both services before changing the supervisor installation or restoring its
database. Restart after validating the updated checkout. A service restart keeps
the durable queue, run records, accepted policy, and Telegram cursor in `.rsi`.

## Queue and release controls

- `/task TEXT` queues development of a candidate; it does not apply its patch.
- `/experiment [GUIDANCE]` queues a bounded evolution experiment. It does not
  automatically promote the best result.
- `/status`, `/best`, and `/diff VERSION_ID` inspect state through supervisor
  handlers. Replies are truncated to fit a Telegram message.
- `/pause` persists a scheduling pause. It lets an already active job finish.
- `/cancel JOB_ID` records cancellation; an active supervisor observes the flag
  and stops its subprocess tree. A waiting job becomes cancelled immediately.
- `/resume` explicitly permits scheduling again after review of a manual, quota,
  or authentication pause.
- `/promote VERSION_ID` and `/rollback VERSION_ID` queue explicit version-change
  requests and refuse acceptance while a worker is active. If scheduling is
  paused, these requests also wait for `/resume`.

The bot commits a control action and the Telegram update cursor in one SQLite
transaction before sending a reply. If delivery of that reply fails, the action
is retained and is not repeated after restart. Inspect `/status` or local job
records when a reply was lost.

Jobs retain their owner, lease, attempt count, cancellation flag, and outcome.
An expired lease marks work interrupted, preserves artifacts, and never
automatically repeats it. The OS worker lock and process ownership reconciliation
must complete before another job starts. Authentication/quota failures pause new
work; only eligible transient infrastructure failures support finite retries.
Review interrupted work before submitting a new request.

Equivalent local commands are `rsi jobs`, `rsi job JOB_ID`, `rsi pause`,
`rsi resume`, and `rsi cancel JOB_ID`, with `--repo /srv/rsi` before the
subcommand. `rsi retry JOB_ID --max-attempts 3` supports only eligible failed
infrastructure jobs, and still respects scheduling pauses. Use
`rsi policy promote VERSION_ID` or `rsi policy rollback VERSION_ID` for an
explicit local accepted-version change after stopping the worker service.
The service owns the worker lock even while idle. While it is running, submit
version changes through Telegram or `rsi enqueue --promote VERSION_ID` and
`rsi enqueue --rollback VERSION_ID` instead.

Observed model tokens accumulate in persistent scheduling state across worker
restarts. Reaching the local cap or receiving unknown usage pauses new work.
Explicit resume clears the accumulated local counter and permits a new budget
window; it does not reset any provider subscription limit. Review account
availability before resuming a quota or authentication pause.

Use manual supervisor commands under the same `rsi` account. Accepted-version
changes must use the supervisor's serialization controls; editing an active
policy file or its database pointer by hand bypasses those controls.

## Retention and backups

Stop the worker service before retention, even when no job is active:

```bash
sudo systemctl stop rsi-worker.service
sudo -u rsi -H /srv/rsi/.venv/bin/rsi --repo /srv/rsi prune --keep 8
sudo systemctl start rsi-worker.service
```

Retention should preserve accepted versions, active jobs, patches, ancestry,
scores, and reproducibility records; the command reports what it removed. It
does not guarantee a fixed disk size when protected artifacts accumulate. Inspect
free space and back up the checkout and `.rsi` state regularly. Stop both services
before a file-based database backup, or use SQLite's backup API; copying only a
live database file can miss committed WAL state. Keep Codex credentials and the
Telegram environment file outside shared artifact backups.

## Evidence still required on the Pi

Record results separately for local unit tests, sandbox execution on Linux ARM64,
one real authenticated Codex task, service restart/cancellation, and a measured
baseline-versus-candidate comparison. Retained state and implemented controls do
not by themselves demonstrate useful self-improvement.
