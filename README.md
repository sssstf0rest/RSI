# RSI seed

A small Python supervisor for developing an evolving personal agent with
official Codex and your saved ChatGPT login. It runs locally or on a Raspberry
Pi 5 with 64-bit Linux; model inference happens in the cloud.

This version develops candidates in separate Git worktrees, records history
in SQLite, independently runs checks, and applies changes after your review.
Telegram is the first capability for the seed to develop; it is not implemented
yet. Automatic scheduling, release, restart, and model training are later work.

## Raspberry Pi setup

Requires Python 3.11+, Git, the current official Codex CLI, and its working OS
sandbox. Package commands below assume Raspberry Pi OS or Ubuntu.

The bootstrap must be committed and pushed before a new clone can obtain it.
Its local development branch is **codex/bootstrap-seed**.

~~~bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl
git clone https://github.com/sssstf0rest/RSI.git
cd RSI
# If the bootstrap is still on its feature branch:
git switch codex/bootstrap-seed
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
~~~

Install Codex using its [official installation instructions](https://learn.chatgpt.com/docs/cli).
The official installer provides Linux ARM64 builds. Keep it current: the seed
uses the current, platform-selecting **codex sandbox** command.

~~~bash
codex login --device-auth
codex login status
rsi doctor
~~~

Complete sign-in in a browser on your phone or laptop. Enable device-code login
in your ChatGPT security settings if required.
[Headless authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
is managed by Codex; RSI does not copy credentials into this repository.

The doctor checks saved ChatGPT authentication and a harmless sandbox probe,
without calling a model. Resolve failed checks first. Verification never falls
back to running candidate code without the sandbox.

This personal runner explicitly selects ChatGPT login. It removes API billing
overrides and bot credentials from the worker environment. Runs consume your
Codex allowance. Keep this runner private rather than placing subscription
credentials in public CI.
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
documents its saved-login behavior.

## First development task

Start from a clean, committed checkout:

~~~bash
rsi run --task-file tasks/telegram.md
~~~

The task includes concrete behavior and acceptance criteria. You can also use:

~~~bash
rsi run --task "Add a notes capability in src/rsi_notes, with tests."
~~~

The command prints a run ID, then starts one bounded Codex task. The default
timeout is 15 minutes. Inspect and verify the result:

~~~bash
rsi runs
rsi show RUN_ID
rsi diff RUN_ID
rsi verify RUN_ID
~~~

After reading the diff and successful check results, apply it explicitly:

~~~bash
rsi apply RUN_ID
git diff
git status --short
# Stage only the intended capability files, tests, and documentation.
git add src/rsi_telegram tests/test_telegram.py README.md
git commit -m "Add Telegram capability"
python -m pip install -e .
~~~

Generated file names may differ; use git status to select the actual files.
Application does not commit, push, install dependencies, or start a service.
The next development run needs a clean commit. Roll back a release by reverting
its commit, reinstalling the editable package, and restarting its service.

New capabilities belong in packages named src/rsi_*. The candidate path
protects the seed source, all existing regression tests, package configuration and release
settings. Core changes require a normal developer review outside this path.

## Telegram activation

The provided task asks the agent to create a package runnable with
**python -m rsi_telegram**, mocked tests, and systemd deployment instructions.
After applying it, you supply:

- TELEGRAM_BOT_TOKEN: a token created through BotFather.
- TELEGRAM_ALLOWED_USER_ID: your numeric Telegram user ID.

Keep them in a private environment file for the generated service. Do not put
them in prompts, source, Git, or task history. The first bot should accept
private /status and /task commands only from you. Tasks propose changes; they
do not automatically release them or restart the bot.

## State and recovery

The ignored .rsi directory contains state.sqlite3, an operation lock, candidate
worktrees, and per-run task text, worker output, check logs, and candidate.patch.
One mutating operation runs at a time.

Checks come from rsi.toml in the main checkout and run as argument lists in a
separate sandboxed process with network disabled. Applying requires the exact
patch that passed verification, an unchanged base commit, and a clean checkout.

The default checks are unittest discovery. Passing them means these tests
passed; task-quality benchmarks are still needed before automatic evolution.
Worktrees separate revisions; the OS sandbox enforces execution restrictions.
This supervised prototype is not a containment system for hostile code.

Codex itself can contact the cloud model. Generated shell commands and checks
cannot access the network. Provision dependencies and live services separately.

Quota errors, timeouts, and failed tests retain the candidate and logs. There
is no automatic retry loop. Ctrl+C terminates the worker process tree. After an
unexpected supervisor crash, the next operation marks unfinished records
interrupted; confirm old workers have stopped before retrying.

## Development

~~~bash
python -m pip install -e .
python -m unittest discover -s tests -v
~~~

Tests use a fake model worker, real temporary Git repositories, and real
Python check processes. They do not consume model usage or contact Telegram.
Only their own test fixtures run with the sandbox replaced. Test success does
not establish that a deployment machine's sandbox works: run rsi doctor and
one real candidate workflow on the Pi.

Next milestones: verify Pi deployment; build and review Telegram; add a
persistent queue and cancellation; measure task-quality improvements; then
allow automatic release for a narrow, evaluated class of changes.
