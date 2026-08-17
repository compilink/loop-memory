---
name: managing-loop-memory
description: Use when an Agent needs to initialize, locate, read, update, migrate, promote, diagnose, archive, or maintain machine-local Loop engineering memory across projects, sessions, or agents, especially after compaction, resume, handoff, or legacy-path discovery.
---

# Managing Loop Memory

## Boundary

Use the shared `loop-memory` CLI. Its only authority root is
`~/loop-memory`, shared by local Agents running as the same operating-system
user. Trust only identities, capabilities, notices, and paths returned by the
CLI. Never create a repository fallback or redirect Loop state into
product-memory storage; product memory remains opaque and complementary.

## Hot Path

1. Call `enter` with the actual cwd, host session ID, optional host-known
   project root, and the current subagent ID when applicable. Do this before
   each ordinary operation and again after resume, compaction, handoff, or a
   cwd, worktree, or project change.
2. If `environment_access_denied` returns typed `required_access`, ask the host
   for exactly `~/loop-memory` read and write access, then retry once. If the
   request is refused, unsupported, changes path or modes, or the retry fails,
   stop that operation without broadening access.
3. Require `ok=true`; honor every returned `capabilities` value and `degraded`
   notice by scope. An unrelated degraded capability does not disable an
   available one. Read only the smallest relevant returned paths. If
   `session_memory_reinitialized` is returned, continue with the returned
   empty session paths; the old working state was already unrecoverable.
4. Write through `session-write` or `promote`, then verify the direct result.
   Keep `status` as live state and `handoff` as a compaction, transfer, or close
   snapshot. Do not write when no durable learning or resumable state changed.
5. On every task, let `enter` validate the canonical global long-memory shape
   and fact index. If it reports `global_long_organization_due`, keep reading
   the returned `global/long.md` (global read remains available), then prepare
   a canonical methodology file and run `global-organize` in the same task.
   This archives the exact previous long file and publishes the concise form;
   it is an actionable convergence hint, not a periodic scheduler.
6. Use `doctor` only to explain notices or recovery; never repair internal
   files manually.

## Ownership and Judgment

- External legacy sources are read-only. The CLI may copy verified bytes into
  Loop custody, but no Loop workflow rewrites or deletes the external source.
- The main Agent owns shared status, handoff, promotion, and subagent inboxes.
  A subagent reads its inbox and writes only its own outbox. Main verifies each
  candidate; each actor clears only its own resolved outbox. Resolve all
  outboxes before `session-close`.
- Promote project or global knowledge only when evidence and the returned
  capability support it. Conflicts, suspected credentials, deletion of unique
  evidence, and unauthorized methodology changes require a human decision.
- Keep `~/loop-memory/global/long.md` as the mandatory global context: it holds
  methodology and a pointer to `~/loop-memory/global/facts/index.md`, not the
  full global fact bodies. Promote a verified fact with `--scope global-fact`;
  the CLI stores its content under `global/facts/entries/` and adds only a
  summary, locator, and digest to the index.
- Update global `AGENTS.md` and global long-term rationale in one methodology
  loop; mark superseded rationale instead of duplicating it.

Read [references/operations.md](references/operations.md) for command syntax,
JSON contracts, capabilities, legacy handling, lifecycle, and diagnosis.
