---
name: governing-subagents
description: Use when Codex is about to delegate work, has open or completed subagents to disposition, resumes a task with historical descendants, or must control subagent count, reuse, closure, handoff, or task-tree growth.
---

# Governing Subagents

Use this governance gate before each delegation batch and while any delegated Agent remains open. It overrides a workflow that would otherwise grow the current task tree unsafely.

## Audit the Tree

1. Inventory the full known task tree with the host's Agent and task-state tools. Classify each descendant as running, waiting, completed, closed, archived, or unknown/unreadable. UI activity labels are hints, not authoritative state.
2. Count open Agents separately from cumulative descendants. Closing an Agent releases concurrency but does not erase descendant history.
3. Preserve unknown or unreadable work. Do not clean it up destructively, and create no new Agents until its state or a reliable conservative count is established.

## Gate Delegation

- Keep at most 8 delegated Agents open concurrently.
- Treat 40 cumulative descendants as a warning, 45 as the fan-out stop threshold, and 50 as a hard ceiling that the workflow must never approach or cross.
- Before starting a task, reserve its full plausible Agent budget: implementer, planned reviewers, and likely repair and re-review work. If the conservative worst-case total would reach 45, do not start it in this tree; hand off first.
- Prefer no delegation when the work is small or tightly coupled. Reuse the same implementer for fixes and the same relevant reviewer for re-review. Do not create new Agents for correction loops merely to follow a template.

## Accept and Close

For each result:

1. Verify the result and cited evidence.
2. If Loop Memory is available, disposition the Agent outbox through `managing-loop-memory`; if unavailable, record the limitation without bypassing its safety gate.
3. Accept or reject the result, then close the Agent immediately when it is no longer needed.

Do not close Agents solely because the UI looks inactive. Do not delete or archive historical tasks without explicit authority.

## Stop and Hand Off

When the count is unsafe, unknown, or the reserved budget reaches the stop threshold:

1. Stop further fan-out.
2. Integrate usable current results and close verified terminal children.
3. Leave a concise, resumable handoff with objective, verified state, evidence, remaining work, and risks.
4. Request or create a fresh top-level task only when the user has granted the required authority.

Apply this gate before `subagent-driven-development` or similar delegation workflows; their normal fan-out must yield to these limits.
