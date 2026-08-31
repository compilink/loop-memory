---
name: managing-loop-memory-runtime
description: Runtime contract for the shared Loop Memory CLI and lifecycle adapters.
---

# Loop Memory Runtime

The runtime implements `managing-loop-memory`; it is not a second memory
authority. The only data root is `~/loop-memory`, shared by Agents of the same
operating-system user. External legacy sources are read-only and product
memory remains separate.

## Entry and capabilities

Call `enter` with actual cwd, host session ID, project root when known, and
subagent ID when applicable. Re-enter after resume, compaction, handoff, or a
project/cwd change, and before the first write or close. Trust only returned
identity, paths, capabilities, and notices. Read a path only when its returned
capability permits it.

For `environment_access_denied`, request exactly the returned `~/loop-memory`
read/write access and retry once. If that fails, block Loop Memory writes,
promotion, migration, and irreversible side effects; read-only diagnosis and
recoverable local work may continue. `degraded` is scoped: an unrelated notice
does not disable a true capability.

## Memory horizons

`global/long.md` is mandatory methodology plus the fact-index pointer. Project
`project/long.md`, `project/medium.md`, and `project/short.md` contain overall
goals and durable facts, current-phase goals/facts, and current-task
goals/progress. Session `status.md`
is live state; `handoff.md` is for compaction, transfer, or close. A legacy
`project.md` is only a compatibility aggregate.

Ordinary work reads project `short.md`, then `medium.md`, then `long.md`.
Progress review, completion, or correction reads `long.md`, then `medium.md`,
then `short.md`. Read full fact bodies only after an index summary and locator
show a matching next action.

## Writes and lifecycle

Write through `session-write` or `promote` only for durable change. Keep a
checkpoint short (goal, done, next, blocker, evidence), reject empty/template-
only or oversized content, skip unchanged writes, and lazily create scoped
files on first meaningful write. The main Agent verifies each promotion
candidate and resolves shared outboxes; a subagent writes only its own outbox.
Do not write when no durable learning or resumable state changed.

Use `doctor` to explain typed notices and `session-close` only after outboxes
are resolved. Run `global-organize` in the same task when `enter` reports
`global_long_organization_due`; do not repair Loop internals manually.

Read [references/operations.md](references/operations.md) for CLI syntax,
contracts, capabilities, migration, lifecycle, and diagnosis.
