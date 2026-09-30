# First capability: a private Telegram interface

Build a Telegram interface for this RSI seed in a new src/rsi_telegram/
package, started with python -m rsi_telegram. Use Python's standard library.
Read src/rsi/core.py to understand the public Seed interface. Preserve the
protected seed and its existing tests.

Required behavior:

- Read TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID from environment
  variables. Fail clearly if either is missing. Never print or persist the
  token, including inside URLs, HTTP exceptions, or logs.
- Use Telegram Bot API long polling. Do not require a public inbound port.
- Accept commands only from the configured numeric user ID in a private chat.
  Reject other users and group chats before reading history or starting work.
- Provide /start, /help, /status and /task followed by a development task.
- /task calls the seed's candidate workflow in a background worker. Keep
  polling while a task runs; allow one active development task at a time.
  Report its run ID, completion status, and where to review it.
- /status reports recent runs without revealing task content to other users.
- A task creates a proposed candidate. It never automatically verifies,
  applies, commits, pushes, installs dependencies, or restarts the service.
- Persist Telegram update offsets. Avoid replaying development tasks after a
  restart by keeping a durable record keyed by update_id. Document recovery
  behavior if a crash happens between accepting and starting a task.
- Bound HTTP waits, retry transient errors with backoff, and shut down cleanly
  on SIGTERM. Do not send task logs or source files to Telegram by default.
- Add mocked tests for unauthorized users, group chats, missing configuration,
  duplicate updates, concurrent tasks, timeout/retry behavior, status and task
  dispatch. Tests must not use a real Telegram account or network.
- Add Raspberry Pi setup instructions and a systemd service example that uses
  a separately provisioned environment file. Do not include real credentials.
- Do not start the bot or contact Telegram during development. A human will
  create a bot through BotFather, supply the token and user ID, inspect the
  candidate, and activate the service.

Acceptance:

1. The existing seed regression tests still pass.
2. The new behavior is exercised by the default unittest discovery command.
3. Missing credentials produce a clear configuration error.
4. Every development request remains subject to the seed's sandbox, timeout,
   isolated worktree and manual application process.
