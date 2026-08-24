# AGENTS.md

This file defines cross-repository working agreements for local Agents. Keep repository `AGENTS.md` files focused on project rules, verification, deployment, and document indexes.

## Loop Engineering

### Core method

- Use progressive disclosure: read only the smallest guidance, context, code, tests, and configuration that unblocks the next useful step.
- For consequential or ambiguous work, separate verified facts, assumptions, interpretations, and decisions; derive the smallest material invariants and directly verifiable acceptance conditions from the objective and constraints.
- Among paths that preserve those invariants, use Occam's razor: choose the fewest unsupported assumptions, shortest useful path, lowest cost, and smallest failure surface. Simplicity is not evidence; never discard facts, safety, data integrity, compatibility, or expected answers merely to be shorter.
- After understanding the requirement, call chain, and root cause, check in order: no new code, existing implementation, standard library, platform-native capability, installed dependency, then the smallest new code. This ladder shortens the solution, not understanding, verification, or required safeguards.
- Keep one-off code local; do not add abstraction, configuration, or future flexibility without a current acceptance condition.

### Standard loop

1. Read the relevant guidance and context.
2. Plan the next useful step.
3. Act with focused edits or commands.
4. Verify the claim with the smallest direct check.
5. Record only durable learning.
6. Leave a resumable handoff when state changed.

### Loop Memory

- Use `managing-loop-memory` for every Loop Memory operation. Its only authority root is `~/loop-memory`; never create a repository or product-memory fallback.
- Call `enter` with actual host context before ordinary operations and after compaction, resume, handoff, or project changes. If typed access is denied, request exactly `~/loop-memory` read/write and retry once; if it still fails, stop Loop Memory writes, promotion, migration, and irreversible external side effects; read-only diagnosis and recoverable local work may continue.
- Trust only returned identities, paths, capabilities, notices, and status. Continue through degradation only where the returned capability remains true.
- Keep product memory complementary and product-managed. Share project facts at project scope, isolate session/Agent state, and let the main Agent verify shared promotion and resolve outboxes.
- Keep `status` as live state and `handoff` as a compaction/transfer/close snapshot. Treat external legacy sources as read-only evidence.
- Keep `~/loop-memory/global/long.md` limited to reusable methodology and the fact-index path. Store full global facts in content-addressed entries and organize an overfull/non-canonical long file during the same task via the returned `global-organize` action; do not edit Loop internals manually.
- Use `doctor` to explain typed blocks, not as a periodic correctness gate. Update this guidance and durable global rationale together when methodology changes.

### Task Scope Governance

- Use `governing-task-scope` when a consequential task can drift in objective, milestone, acceptance, scope, or Agent budget.
- Persist its versioned contract in the resolved session and re-evaluate it at execution proposals, delegation, milestone transitions, and completion.
- Stop when current acceptance is satisfied. If evidence changes the objective, acceptance semantics, or authorized change surface, record a handoff and open a new contract version instead of extending the current work.
- User instructions and the applicable `AGENTS.md` chain outrank the contract; automatic correction may not change objectives, erase evidence, or defer security, data-integrity, or compatibility risks. The layer is workflow-neutral; Superpowers is optional.

### Subagent governance

- Before delegating, and while delegated Agents remain open, use `governing-subagents`. Main verifies evidence, accepts or rejects outboxes, and closes Agents promptly. Stop fan-out and hand off when the task tree is unsafe or unknown.

### Read order and completion

At substantive-task start: read this file, the closest repository guidance, then `enter`; read only returned paths permitted by capabilities before the smallest relevant project surface. For conflicts, prefer the user, then the closest `AGENTS.md`, then broader methodology; for facts, prefer direct runtime or test evidence over remembered summaries.

Before claiming completion, run the command that proves the claim, update live status or handoff when durable state changed, and keep global guidance lean. Commentary records milestones and blockers, not primary task state.
