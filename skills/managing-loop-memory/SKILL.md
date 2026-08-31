---
name: managing-loop-memory
description: Use when an Agent needs to initialize, locate, read, update, migrate, promote, diagnose, archive, or maintain machine-local Loop engineering memory across projects, sessions, or agents, especially after compaction, resume, handoff, or legacy-path discovery.
---

# Managing Loop Memory

## Boundary

Use the shared `loop-memory` CLI. Its only authority root is `~/loop-memory`,
shared by Agents of the same operating-system user. Trust only returned
identities, capabilities, notices, and paths. Never create a repository
fallback or redirect Loop state into product memory.

## Entry and recovery

1. Call `enter` with actual cwd, host session ID, optional project root, and
   subagent ID. Repeat after resume, compaction, handoff, cwd/worktree change,
   and before the first write or close.
2. Require `ok=true` and read only capability-authorized paths. For typed
   `environment_access_denied`, request exactly the returned `~/loop-memory`
   read/write access and retry once. If recovery fails, stop Loop writes,
   promotion, migration, and irreversible side effects; diagnosis and
   recoverable local work may continue. Treat degradation by scope.

## Memory layers and progressive disclosure

Keep layers small and non-overlapping:

- `global/long.md` is mandatory methodology plus the fact-index pointer;
  complete global facts live in the indexed archive. Global medium is
  provisional cross-project rationale; global short is compatibility-only.
- `project/long.md`, `project/medium.md`, and `project/short.md` hold overall
  goals and durable facts, current-phase goals/facts, and current-task
  goals/progress. A returned legacy `project.md` is only a compatibility
  aggregate.
- Session `status.md` is live resumable state; `handoff.md` is the compact
  compaction/transfer/close snapshot. Inbox/outbox files are coordination state.

For ordinary work, read project `short.md`, then `medium.md`, then `long.md`.
For progress review, completion, or correction, read `long.md`, then `medium.md`,
then `short.md`. Read full fact bodies only after an index summary and locator
show relevance.

## Writing and lifecycle

- Write only for durable change: milestone, blocker, handoff, compaction, or
  close. Keep checkpoints short (goal, done, next, blocker, evidence); do not
  copy conversation or internal reasoning.
- Write through the CLI; verify `exit=0`, `ok=true`, identity, and returned path
  or change. Reject empty/template-only or oversized bodies, skip unchanged
  writes, and lazily create scoped files on first meaningful write.
- Promote evidence-backed knowledge only. Keep inference in the medium layer or
  current outbox; the main Agent verifies each promotion candidate and resolves
  outboxes. External legacy sources are read-only.
- Use `doctor` for typed notices; never repair Loop internals manually. If
  `enter` returns `global_long_organization_due`, run `global-organize` in the
  same task.

Read [references/operations.md](references/operations.md) for command syntax,
contracts, capabilities, migration, lifecycle, and diagnosis.
