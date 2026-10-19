# Policy execution boundary

The supervisor copies eligible policy assets into a fresh execution workspace.
It starts an isolated Python child through the official Codex sandbox, supplies
only validated JSON, and reads validated output. It never imports a candidate
policy into its own process. A restricted profile denies filesystem reads by
default, permits the workspace and interpreter runtime, makes policy source
read-only, permits a local temporary directory, and disables direct network
access. Expected evidence labels and evaluation reports remain outside the
worker workspace.

On Linux, the profile also permits the exact installed Codex executable and its
bootstrap helper directories (`CODEX_HOME/tmp/arg0` and `tmp/arg0-linux`) read-only.
For npm installations, the native executable is resolved separately from the
JavaScript launcher, including nested and hoisted platform packages. The rest of
the isolated Codex home stays denied. Older bubblewrap versions, including the
one supplied by Ubuntu 22.04, need the live `codex-linux-sandbox` PATH alias during
startup; hiding it causes `bwrap: execvp codex-linux-sandbox: No such file or directory`.
Newer bubblewrap versions also need access to the native executable itself.
See the official [helper re-execution logic](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/linux-sandbox/src/linux_run_main.rs#L500-L544)
and [npm launcher](https://github.com/openai/codex/blob/rust-v0.162.0/codex-cli/bin/codex.js#L93-L113).

Restricted profiles extend `:read-only`, override the default root access to
`deny`, and allow only their workspace, local temporary directory and runtime
paths. They explicitly preserve read-only `.git`, `.agents`, `.codex` and `.aws`
metadata. Inheriting `:workspace` would also inherit global temporary-directory
writes. Undoing those grants with `:slash_tmp = "deny"` causes another Linux
startup failure: Codex applies the `/tmp` mask after mounting read-only paths,
which hides helper aliases and policy workspaces beneath `/tmp`. Denial by
default avoids both the broad write grant and that ancestor mask. See the
[upstream mount order](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/linux-sandbox/src/bwrap.rs#L539-L646).

Unit tests substitute sandbox command execution only for small fixture programs.
They verify protocol behavior and evaluation but cannot establish OS isolation.
Evaluator and evolution tests use a frozen baseline in `tests/fixtures` so their
expected comparisons remain stable when checks run from an improved policy's
candidate worktree. The live candidate still passes protocol and behavioral
evaluation separately.
The additional integration test uses the actual sandbox without model inference:

```bash
RSI_REAL_SANDBOX=1 python -m unittest discover -s tests -p test_sandbox_integration.py -v
```

It verifies ordinary policy execution, denial of known outside and private Codex
home canary reads, denial of policy-source writes, and successful local
temporary-file writes.
An unsupported sandbox fails this opt-in check; it does not fall back to direct
execution. No ChatGPT login or API key is needed for this local test.

## Verification environments

The CI configuration runs ordinary tests on Linux and Windows with Python 3.11
and 3.13. A separate Ubuntu 22.04 job installs official Codex and bubblewrap,
runs the real boundary checks, and completes an offline evolution experiment
through the production sandbox. Both steps must pass. On failure, CI prints the
nested regression log. Ubuntu 22.04 CI does not establish
compatibility with Raspberry Pi ARM64 or Ubuntu Server 24.04.

On the development Windows host, Codex CLI 0.163.0-alpha.10.2 rejected the
restricted profile before starting Python:

```text
windows sandbox failed: Restricted read-only access requires the elevated Windows sandbox backend
```

A per-command MXC probe also returned that guard. This is an infrastructure
failure, not a candidate score or successful isolation check. No administrator
provisioning, machine policy changes, or model inference was performed. The
official Windows documentation distinguishes MXC, elevated, and unelevated
backends; the unelevated backend cannot enforce denied read paths. Provision a
supported backend before running these policies on Windows.

On Ubuntu 24.04, install distribution bubblewrap and follow the official targeted
AppArmor guidance if namespace restrictions prevent startup. Do not globally
disable AppArmor or unprivileged namespace restrictions as a workaround. Run
`rsi doctor` and the integration test on the intended Pi service account before
claiming deployment verification.

Source-checked on 19 October 2026:

- [Official permission profiles](https://learn.chatgpt.com/docs/permissions)
- [Official sandboxing guidance](https://learn.chatgpt.com/docs/sandboxing)
- [Official Windows sandbox configuration](https://learn.chatgpt.com/docs/windows/windows-sandbox)

These controls are execution and evaluation boundaries for a supervised personal
agent. They do not establish protection against every hostile native program,
OS exploit, resource-exhaustion attack, or sandbox implementation defect.
