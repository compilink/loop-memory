# AGENTS.md

Use the skills below when their trigger is present; keep this file limited to skill triggers. The skills contain the operational details.

## Loop Memory

- For any Loop Memory initialization, entry, read, write, promotion, migration, diagnosis, archive, or maintenance, use `managing-loop-memory`.
- At task start, and after resume, compaction, handoff, or a project/cwd change, invoke `managing-loop-memory` again before relying on memory.

## Task Scope

- For consequential work that may drift in objective, milestone, acceptance, scope, or budget, use `governing-task-scope`.

## Completion

- Before claiming completion, run the command that directly verifies the claim and leave a resumable memory handoff when durable state changed.
