# Policy execution boundary

The supervisor copies eligible policy assets into a fresh execution workspace.
It starts an isolated Python child through the official Codex sandbox, supplies
only validated JSON, and reads validated output. It never imports a candidate
policy into its own process. A restricted profile denies filesystem reads by
default, permits the workspace and interpreter runtime, makes policy source
read-only, permits a local temporary directory, and disables direct network
access. Expected evidence labels and evaluation reports remain outside the
worker workspace.

Unit tests substitute sandbox command execution only for small fixture programs.
They verify protocol behavior and evaluation but cannot establish OS isolation.
The additional integration test uses the actual sandbox without model inference:

```bash
RSI_REAL_SANDBOX=1 python -m unittest discover -s tests -p test_sandbox_integration.py -v
```

It verifies ordinary policy execution, denial of a known outside canary read,
denial of policy-source writes, and successful local temporary-file writes.
An unsupported sandbox fails this opt-in check; it does not fall back to direct
execution. No ChatGPT login or API key is needed for this local test.

## Verification environments

The CI configuration runs ordinary tests on Linux and Windows with Python 3.11
and 3.13. A separate Ubuntu 22.04 job installs official Codex and bubblewrap and
runs the real boundary checks. This workflow has been prepared locally; a green
CI result must be observed after publication. Ubuntu 22.04 CI does not establish
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
