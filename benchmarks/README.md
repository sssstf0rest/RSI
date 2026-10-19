# Synthetic context smoke evaluation

`context.json` is a small, manually labelled engineering-notes exercise. Its 12
development and 6 selection queries are synthetic, public, and intentionally
simple. They demonstrate the evaluator and candidate boundary; they are not a
scientific benchmark, independent generalization evidence, or foundation-model
evaluation. Every supplied case is available to repository contributors.

The trusted evaluator computes precision, recall, evidence F1, exact document-set
success, context length, and runtime from the actual selected documents. It never
sends expected IDs, case IDs, split labels, or scores to policy workers. Selecting
every document is penalized by precision. Only complete document blocks that fit
the context budget are passed to the model.

Policies receive JSON containing `query`, `documents` (`id` and `text` only),
`experience`, and `max_context_chars`. They emit exactly `selected_ids` and
`instructions`. Policy source executes in a separate Codex sandbox with a
filtered environment. The supervisor constructs context; policy claims about
scores are rejected.

`examples/lexical/policy.py` is a deterministic improved lexical example. It is
not automatically active. Compare it to `src/rsi_policy/policy.py` on development
cases first; evaluating or using selection failures to tune it consumes that
selection split as additional development feedback. No supplied cases are truly
held out once used for candidate selection or improvement.

Final-audit cases must be separately provisioned outside this directory and kept
outside the proposing worker's readable filesystem. `ContextEvaluator` rejects
public `audit` cases; a trusted caller must explicitly construct it with
`external_audit=True` and invoke `evaluate_audit`. Provisioning a file alone does
not establish an independent audit. A final comparison should use fresh tasks,
the same model/reasoning settings and comparable cumulative model usage.
