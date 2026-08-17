# AGENTS.md

This file provides global methodology guidance for Codex, Claude Code, and other local agents on this machine.

Use it for cross-repository working agreements. Keep repository `AGENTS.md` files focused on project-specific rules, verification commands, deployment constraints, and document indexes.

## Loop Engineering

### Progressive Disclosure

- Start with the smallest instruction and document set that can unblock the next useful step.
- Read only the next relevant layer of guidance, context, code, tests, and config.
- Promote durable lessons instead of expanding prompts or chat recaps.

### First Principles and Occam's Razor

- Use first-principles reasoning for consequential, ambiguous, or complex work; use proven conventions for routine decisions unless they hide material assumptions.
- Start from the objective, constraints, and verified facts. Separate facts, assumptions, interpretations, and decisions; never treat preference or an untested belief as a first principle.
- Decompose the problem into the smallest verifiable invariants, derive viable directions without being constrained by the current process or implementation, then use experience to validate and optimize.
- Among solutions that preserve material facts and sufficient explanatory power, prefer the fewest unsupported assumptions, shortest execution path, lowest cost, and smallest failure surface. Remove work that does not serve the objective.
- Simplicity is a selection rule, not evidence of truth. Do not over-simplify, discard a stronger complex explanation, guess across evidence gaps, or change expected answers merely to pass a gate.

### Standard Loop

Every meaningful work loop should follow:

1. Read the relevant guidance and context.
2. Think and plan the next useful step.
3. Act with focused edits or commands.
4. Verify with the smallest command that directly proves the claim.
5. Record durable learning when it will help a future loop.
6. Leave a resumable handoff.

Substantive work means work that changes durable state, spans multiple verifiable steps, may survive a context transition, or creates reusable learning. A direct answer or read-only check that changes none of these does not require a full persistence loop.

### Loop Memory

- Use `managing-loop-memory` for every Loop Memory operation. Its only authority root is `~/loop-memory`; never create a repository fallback or redirect Loop state into product-memory storage.
- Call its `enter` workflow with the actual host context before ordinary Loop operations and after compaction, resume, handoff, or a working-directory, worktree, or project change. If typed `required_access` is returned, request exactly `~/loop-memory` read/write and retry once.
- Trust only returned identities, paths, `capabilities`, and notices. Continue through scope-specific degradation only with capabilities that remain true. Commentary, summaries, and remembered paths are not authoritative task state.
- Codex product memory is complementary evidence and remains product-managed. The Loop skill neither inspects nor manages product memory internals.
- Share project and worktree knowledge at project scope while isolating session and Agent state. Subagents submit evidence-backed candidates through their outbox; the main Agent verifies, accepts, and promotes them.
- Treat session `status` as live state and `handoff` as a compaction, transfer, or close snapshot. Resolve all outboxes before `session-close`.
- Treat external legacy sources as read-only; import may copy verified bytes into Loop custody but never rewrite or delete the source.
- Keep `~/loop-memory/global/long.md` as the mandatory global context. It must contain only reusable methodology and the path `~/loop-memory/global/facts/index.md`; full global facts belong in content-addressed `global/facts/entries/` files, while the index carries concise summaries and exact locators.
- On every task, `enter` validates the long-memory and fact-index invariants. A non-canonical or overfull long file returns the non-blocking `global_long_organization_due` notice. In that same task, prepare the canonical methodology and invoke `global-organize`; it archives the exact prior long file with a receipt and atomically publishes the concise form. This per-task convergence replaces periodic governance and never requires manual edits to Loop internals.
- Use `doctor` to explain a typed block, not as a periodic correctness dependency.
- Treat this file as the normative methodology source and global long-term Loop memory as durable rationale. Update both in one methodology loop; mark superseded rationale explicitly, and consolidate it only when canonical storage invariants allow.

### Read Order

At the start of substantive work:

1. Read this file, then the closest applicable repository `AGENTS.md`.
2. Invoke `managing-loop-memory` `enter` with the actual current context and read only returned files whose capabilities permit the operation.
3. Follow repository guidance for additional project documents and verification requirements.
4. Read the smallest relevant code, tests, and config surface.

For normative conflicts, prefer the user request, then the closest applicable `AGENTS.md`, then broader methodology guidance. For factual conflicts, prefer direct runtime/test/config/code evidence, then current session evidence, verified project knowledge, and secondary documentation. Never let a lower-authority instruction override a higher-authority one, or let document authority substitute for factual evidence.

### Subagent Governance

- Before delegating work, and while any delegated Agent remains open, use `governing-subagents`.
- Close delegated Agents promptly after accepting or rejecting their results.
- When the skill reports an unsafe, unknown, or oversized task tree, stop further fan-out and hand off substantial work to a fresh top-level task.

### Progress and Write Discipline

- Keep non-trivial progress, phase changes, waiting states, next actions, and handoff data in the resolved current session.
- Update shared knowledge only when it will help a future loop. Keep entries dated, concise, evidence-backed, and free of secrets, credentials, raw customer data, and unnecessary PII.
- If no durable learning or resumable state changed, no Loop memory write is required.
- Put project facts in resolved project memory and reusable methodology in resolved global memory. Keep broader architecture, deployment, API, and business material in project documentation.
- The main Agent owns shared promotion and must verify subagent evidence before accepting it.

### Commentary Discipline

- Spend time thinking before speaking.
- Use commentary for useful milestones, blockers, and decisions rather than step-by-step narration.
- Do not rely on commentary or chat history as the primary record of task state.

### Completion Discipline

Before claiming completion:

- run the verification command that proves the current claim, or state why verification does not apply
- update the relevant Loop memory or handoff when the task changed durable state
- keep global guidance lean and move project detail into repository guidance and project docs
