# Global Long-Term Memory

## Methodology

- [2026-08-14][verified] Use progressive disclosure: read the smallest guidance, context, code, tests, and configuration that unblocks the next useful step.
  Evidence: global AGENTS.md methodology.
- [2026-08-14][verified] For consequential or ambiguous work, separate verified facts, assumptions, interpretations, and decisions; derive the smallest verifiable invariants first, then choose the option with the fewest unsupported assumptions and smallest failure surface.
  Evidence: global AGENTS.md first-principles and Occam's-razor rules.
- [2026-08-14][verified] A meaningful loop reads guidance, plans, acts, verifies directly, records only durable learning, and leaves a resumable handoff.
  Evidence: global AGENTS.md standard loop.
- [2026-08-14][verified] Re-enter Loop Memory on every substantive task and after context or project transitions; trust only returned identities, paths, capabilities, notices, and current session state.
  Evidence: installed managing-loop-memory skill and runtime contract.
- [2026-08-14][verified] Keep global long context concise, route complete reusable facts to the indexed archive, isolate project/session/Agent state by scope, and let the main Agent verify shared promotion.
  Evidence: global AGENTS.md Loop Memory and ownership rules.
- [2026-08-14][verified] Before completion, run the smallest command that proves the claim, update durable state when it changed, and leave global guidance lean.
  Evidence: global AGENTS.md completion discipline.
- [2026-08-21][verified] For consequential multi-step work, use a versioned task contract in the resolved Loop Memory session and a deterministic lifecycle guard. Apply first-principles decomposition before Occam selection; record only material invariants, acceptance conditions, and any deliberate simplification's ceiling and reconsideration trigger. Workflow frameworks remain optional adapters, and delegated Agents must re-enter and validate the current contract reference before acting.
  Evidence: governing-task-scope design, scope_guard.py tests, and the standalone skill contract tests in commit 1cf6afd.
- [2026-08-24][verified] The missing anti-overimplementation mechanism was solution selection after root-cause analysis, not another workflow: a fixed reuse ladder reduces unsupported invention while leaving understanding, verification, and safeguards intact.
  Evidence: comparative review of a reuse-first method and Loop Engineering governance.
- [2026-08-24][verified] Scope drift often begins when each newly observed failure is treated as implementation work. Linking work to pending acceptance or invariants, defining a stop condition, and versioning material changes preserves evidence without silently expanding the milestone.
  Evidence: review of a multi-step integration iteration and governing-task-scope contract design.
- [2026-08-24][verified] `AGENTS.md` is the compact executable trigger layer; global long memory stores durable rationale and method summaries, while project and session memory store scoped facts and resumable state. They should evolve together without duplicating each other's operational detail.
  Evidence: global memory shape and installer source-of-truth review.

## Fact Index

- `~/loop-memory/global/facts/index.md`
