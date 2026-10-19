# RSI: a persistent agent with evaluated policy evolution

RSI runs a Python supervisor on a personal computer or Raspberry Pi and uses
official Codex with saved ChatGPT authentication for cloud inference. It develops
capabilities, compares context-selection policies, retains accepted versions,
and accepts private Telegram tasks.

The repository implements the handoff's minimal evolution loop directly, with no
additional research-framework dependency. Shinka/GEPA adapters can be added at
the same evaluator boundary later. No foundation-model training is performed.

## What is implemented

- Structured Codex outcomes, model/effort requests, observed session metadata,
  CLI version, elapsed time, usage, responses and logs.
- Separate candidate Git worktrees, an enforced path allowlist, sandboxed
  checks, and verification of the exact patch being released.
- A JSON policy hook that selects context for subsequent real Codex tasks.
  Candidate Python executes in a separate sandbox, outside the supervisor.
- Fixed behavioral evaluation, baseline comparisons, immutable policy snapshots,
  parent ancestry, explicit promotion and rollback through persistent pointers.
- Relevant experience retrieval, plus a memory-disabled comparison option.
- A single-worker SQLite queue with leases, cancellation, restart recovery,
  idempotent requests, finite explicit retries and persistent usage/quota pauses.
- Private Telegram controls and Linux systemd service templates.
- Bounded mutation-instruction experiments comparing matched descendants from
  the same parent, including proposal costs and strategy versioning.

Tests use mocked model calls and real local Git/Python processes. The supplied
benchmark is an intentionally small synthetic smoke dataset: 12 development
cases and 6 selection cases. It demonstrates a working comparison mechanism,
not improvement on independent real-world tasks.

**Deployment status:** unit/integration fixture tests run locally on Windows.
This host's current Codex sandbox backend refuses the required restricted read
policy. The implementation keeps that restriction and has no weaker fallback.
Linux sandbox CI is configured; target Pi deployment and live OAuth inference
must still be verified. See [sandbox checks](docs/SANDBOX.md).

## Raspberry Pi quick start

Requires Python 3.11+, Git, a current official Codex CLI, and its working OS
sandbox. Ubuntu Server 24.04 ARM64 also needs bubblewrap and may require the
targeted AppArmor profile documented by Codex.

Use a published revision containing these files. The implementation branch is
codex/evolution-loop; it must be published before a fresh GitHub clone can fetch
it.

~~~bash
sudo apt update
sudo apt install git python3-venv curl bubblewrap
git clone https://github.com/sssstf0rest/RSI.git
cd RSI
git switch codex/evolution-loop
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
~~~

Install Codex using the [official CLI instructions](https://learn.chatgpt.com/docs/cli).
Complete [device authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
under the same Linux account that will run RSI:

~~~bash
codex login --device-auth
codex login status
rsi doctor
~~~

The doctor does not call a model. It checks authentication, a clean checkout and
actual sandbox workspace/temp writes. A failed sandbox probe must be resolved
before running candidates. Read its reported error and log path.

The configured model is gpt-6.1-sol with medium reasoning. Confirm availability
on your account and change rsi.toml explicitly if necessary; record the same
settings for baseline and candidate comparisons. Model requests consume the
account's Codex allowance. API billing overrides are removed from workers.

For a dedicated service account, installation commands, Ubuntu/AppArmor guidance,
private Telegram configuration and systemd activation, follow
[the operation guide](docs/OPERATIONS.md).

## First complete improvement loop

Start from a clean committed checkout. This example evaluates an existing local
candidate and makes **no model proposal**. It still executes real sandboxed
regression and behavioral checks.

~~~bash
rsi policy init
rsi evaluate --no-memory
rsi experiment --candidate benchmarks/examples/lexical --no-memory
~~~

The experiment returns a policy version ID, baseline/candidate scores, eligibility
and a promotion decision. Inspect its source run and patch:

~~~bash
rsi policy list
rsi diff VERSION_ID
rsi policy promote VERSION_ID
rsi policy best
rsi evaluate --no-memory
~~~

Promotion switches a persisted immutable policy pointer. It leaves the Git
checkout clean, and the next task loads that version automatically. Restarting
the supervisor preserves the active policy. Previously accepted versions remain
available for rollback:

~~~bash
rsi policy rollback PREVIOUS_VERSION_ID
~~~

The lexical example is designed to outperform the deliberately simple seed on
the synthetic smoke tasks. Treat that result as functional verification. Build
independently labelled tasks from your own authorized notes/code before making
claims about useful generalization.

## Ask the agent to propose an improvement

After the offline workflow and live authentication are verified:

~~~bash
rsi experiment --task "Select relevant configuration and recovery notes; reduce irrelevant context."
rsi experiment --parent PARENT_VERSION_ID --task "Try a different general retrieval strategy."
~~~

Each call creates one bounded proposal, runs fixed regressions, evaluates both
the active baseline and candidate, and records ancestry and provenance. It
does not automatically activate the candidate.

The evolution path can modify only src/rsi_policy/. The broader capability path
allows src/rsi_* Python/Markdown/JSON, new tests, and documentation; existing
regression tests, evaluator files, supervisor code and release configuration
are protected. Generated agent-instruction files are ineligible.

Promotion requires passing regressions, no task-quality regression, improved
selection quality or smaller total policy-added context at equal quality,
known observed usage within the local limit, unchanged trusted inputs, and an
unchanged active baseline. Candidate self-reported scores are never accepted.

## Develop capabilities and use Telegram

~~~bash
rsi run --task "Add a notes capability in src/rsi_notes with meaningful tests."
rsi diff RUN_ID
rsi verify RUN_ID
rsi apply RUN_ID
~~~

Capability application writes a verified patch into the checkout. Review and
commit it before the next task. Policy promotion uses the separate persistent
version pointer and does not need to edit or commit the checkout.

Telegram is already implemented in src/rsi_telegram. Create a bot with BotFather
and provide TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID privately. It accepts
only the configured user in their private chat. A separate queue worker performs
development, so polling remains responsive.

Commands include /status, /task, /experiment, /pause, /resume, /cancel, /best,
/diff, /promote and /rollback. Promotion is an explicit authorized control
command. Accepted updates, queued jobs and offsets are stored atomically;
a failed reply cannot repeat a development task.

## Durable worker and budgets

~~~bash
rsi enqueue --task "Add a small tested helper." --job-id helper-attempt-1
rsi worker --max-jobs 1
rsi jobs
rsi job JOB_ID
rsi cancel JOB_ID
rsi pause
rsi resume
~~~

Repeating an idempotent job ID returns the same request. It cannot identify a
different payload. Interrupted jobs retain their artifacts and are not silently
replayed. Explicit retries are finite and restricted to failed infrastructure
jobs:

~~~bash
rsi retry JOB_ID --max-attempts 3
~~~

A service can wait for queued requests with worker --serve. Its idle state makes
no model requests. The worker owns an OS file lock, records child identities,
heartbeats leases, and reconciles abandoned execution before claiming new work.
Systemd units terminate the whole service control group on shutdown.

Direct development and version changes acquire the same worker lock. While a
service owns it, enqueue supported operations or stop the service for direct
maintenance. Read [operational recovery](docs/OPERATIONS.md) before unattended use.

Current limits in rsi.toml:

| Limit | Default |
| --- | --- |
| Capability task | 3600 seconds |
| Small evolution proposal | 600 seconds |
| Regression check | 300 seconds |
| Each local policy evaluation | 60 seconds |
| Selected document context | 6000 characters |
| Retrieved experiences | Up to 4 |
| Observed model-work scheduling window | 200000 input + output tokens |

Cached input is not counted a second time. An idempotent usage ledger accounts
for persisted executions across worker restarts. Unknown usage and local-cap
exhaustion pause new work; quota/authentication failures also pause scheduling.
Explicit resume resets the local accounting window, not the provider's limits.

This is an observed-work scheduling limit. It is not a hard per-call Codex token
ceiling or a claim about subscription charges/entitlements. A call can cross the
local threshold before its final usage report arrives.

Retire old worktrees and large top-level logs while preserving policy snapshots,
patches, run records, metrics and provenance:

~~~bash
rsi prune --keep 8
~~~

The most recent runs and accepted worktrees, plus the active source worktree,
are retained. Compact policy/strategy histories remain available. Retention is
explicit maintenance; the command never removes an active worker's files.

## Independent audit and memory comparisons

The public benchmark separates development from selection. Once selection
feedback is used to tune a policy, those cases are development evidence.

Provision fresh audit labels outside the repository and readable runtime roots.
The proposing worker and isolated policy workspace must not be able to read
them. The evaluator loads labels in the supervisor and sends only queries and
corpus documents into the policy process.

~~~bash
rsi audit --dataset /private/rsi-audit.json --baseline BASELINE_ID --version CANDIDATE_ID
rsi evaluate --version CANDIDATE_ID
rsi evaluate --version CANDIDATE_ID --no-memory
~~~

External audit data uses the same document/case schema but split="audit".
The API requires explicit audit access. File placement alone does not prove
independence: verify the sandbox and prevent audit failures from entering the
improvement loop. The supplied data has no final-audit cases.

Local retrieval scores do not establish end-to-end coding improvement. A later
real-task comparison must hold model, reasoning settings and resource budgets
comparable, repeat trials, and record task outcomes and intervention.

## Controlled improvement of mutation instructions

After ordinary policy evolution works, propose new mutation instructions:

~~~bash
rsi strategy propose --task "Improve how retrieval failures guide policy changes."
rsi strategy experiment --candidate CANDIDATE_PATH --trials 1
rsi strategy list
rsi strategy promote STRATEGY_ID
~~~

The proposer returns candidate_path and structured execution records. A strategy
experiment compares fixed and candidate instructions through 1–3 matched trials,
starting every descendant from the same policy parent. It disables retrieved
memory for comparison, alternates trial order, includes proposal and descendant
model usage, and keeps descendant policies inactive.

A strategy becomes eligible only after quality and observed-work comparisons
pass. Missing usage, failed evaluations, changed fixed inputs, or incomparable
observed model metadata block activation. Accepted strategy instructions affect
later policy proposals and persist across restarts. Explicit rollback is
available through strategy rollback.

These are small bounded prompt/policy experiments. They do not establish broad
recursive intelligence improvement, causality, or statistical significance.

## Development and validation

~~~bash
python -m pip install -e .
python -m unittest discover -s tests -v
~~~

Ordinary tests mock inference and substitute the sandbox only for test-owned
fixture programs. They use real Git repositories, subprocesses, persisted state,
and behavior grading. Separate opt-in sandbox tests execute the real boundary:

~~~bash
RSI_REAL_SANDBOX=1 python -m unittest discover -s tests -p test_sandbox_integration.py -v
~~~

CI includes Linux/Windows unit matrices and a Linux sandbox job with an offline
production-path evolution experiment. CI configuration is not a record of a
successful CI run. No test requires a real Telegram token.

For the handoff mapping, remaining validation and research limits, see
[implementation status](docs/IMPLEMENTATION.md).
