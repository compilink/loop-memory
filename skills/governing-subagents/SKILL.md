---
name: governing-subagents
description: Use when delegation, open descendants, historical descendants, or subagent result disposition needs governance beyond the host's native task-tree controls.
---

# Governing Subagents

This is a host-neutral supplement, not a replacement for Codex or another
host's host-native task and concurrency controls. Use it only when delegation or
result disposition is in scope and the host does not already provide the
needed authoritative state.

## Before delegation

1. Inspect the host-provided task tree and classify descendants as running,
   waiting, completed, closed, archived, or unknown. UI labels are hints only.
2. Prefer no delegation for small or tightly coupled work. Reuse existing
   Agents when the host permits it. Do not invent fixed capacity numbers or a
   second scheduler; follow the host's reported limits and user instructions.
3. If tree state or limits are unknown, stop new fan-out and leave a concise
   handoff until the host can provide authoritative state.

## While Agents are open

- The main Agent verifies evidence, accepts or rejects results, and closes
  Agents through host-native controls as soon as they are no longer needed.
- When Loop Memory is used, apply `managing-loop-memory` ownership rules:
  subagents write only their own outbox, and the main Agent resolves shared
  state. Never treat a parent prompt or chat summary as authoritative state.
- Preserve unknown or unreadable work. Do not delete or archive task history
  without explicit authority.

## Boundary

If the host already enforces delegation count, closure, or handoff, this skill
adds no duplicate gate. It must not override host behavior, change objectives,
or widen the authorized work surface.
