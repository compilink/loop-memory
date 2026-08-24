---
name: governing-task-scope
description: Use when consequential multi-step work can drift in objective, milestone, scope, review disposition, completion criteria, or Agent budget, especially across plans, delegation, compaction, resume, or handoff.
---

# Governing Task Scope

## Boundary

This is a workflow-neutral governance layer. It uses `managing-loop-memory`,
the main Agent, and the deterministic guard in `scripts/scope_guard.py`. It does
not replace planning, implementation, review, or task-tree governance.

Use this authority order: current user instructions, applicable `AGENTS.md`,
the latest non-superseded task contract, this skill, optional workflows, then
domain skills. Amend the contract version when higher authority changes it;
never silently reinterpret the old contract.

## Establish The Contract

1. Invoke `managing-loop-memory` `enter` with the actual host context. Read only
   the returned `status` when `session_read=true`; remembered paths and chat are
   not task state.
2. If no contract exists, create a discovery candidate. If one exists, treat
   its `contract_id`, version, and digest as current only after the guard admits
   it against the returned status.
3. Keep the complete schema in [references/contract.md](references/contract.md).
   Do not copy requirements, designs, plans, or review reports into the
   contract; reference stable sources and artifact digests.

## First Principles

For consequential or ambiguous work, derive the candidate from the objective,
authoritative constraints, and verified facts. Keep verified facts,
assumptions, interpretations, and decisions distinct. Derive the smallest
material invariants and directly verifiable acceptance conditions. Never
convert an assumption into a fact; resolve it with evidence or keep it open.

## Occam's Razor

Among paths that preserve every material invariant and enough explanatory
power, select the fewest unsupported assumptions, shortest useful path, lowest
cost, and smallest failure surface. Remove work that does not serve the current
milestone. Routine choices may reuse proven conventions when they hide no
material assumption.

Simplicity is not evidence. Do not discard a stronger explanation, omit a
material fact, weaken an expected answer, or remove security, data-integrity,
or compatibility protection to make the plan smaller or pass a gate.

When a selected path deliberately accepts a known limit, add it to
`decision.simplifications` with a summary, current ceiling, and evidence-based
reconsideration trigger. This metadata is not a separate debt system and does
not schedule cleanup.

## Incremental Work Gate

Before admitting a work item at `execution-proposal`, tie it to a pending
acceptance item or invariant, name the verified root cause it addresses, and
state the smallest direct verification and stop condition. Re-check existing
implementation, standard library, platform-native capability, and installed
dependency first. If an item changes input, state, or acceptance semantics, it
is separate work; if current acceptance is satisfied, stop and complete. When
evidence changes the objective, acceptance semantics, or authorized change
surface, record a finding and handoff, then create a new contract version
instead of extending the current work. A failure outside the authorized change
surface is evidence to classify and hand off, not permission to widen
implementation.

## Gate Boundaries

Invoke the script by its resolved absolute path:

```bash
python3 /absolute/skill/path/scripts/scope_guard.py evaluate \
  --event EVENT --candidate CANDIDATE [--current CURRENT] \
  [--authority-index INDEX] [--output APPROVED] --json
```

Use exactly these boundaries:

- `task-start`: establish version 1 before execution.
- `execution-contract`: publish the approved discovery-to-execution contract.
- `execution-proposal`: admit a plan or focused next action.
- `delegation`: validate the inherited current contract and admitted work.
- `review-disposition`: admit `block`, `fix-now`, or permitted `defer` choices.
- `milestone-transition`: require current acceptance before moving on.
- `completion`: require satisfied acceptance and fresh implementation evidence.

Exit `0` with `ok=true` permits `allow` or `correct`; exit `3` blocks with
stable reason codes. For contract events, publish only the guard's approved
output through `loop-memory session-write --kind status`. Re-enter before the
write and verify the returned identity and path. Do not edit Loop Memory
internals. A gate never determines truth or runs project verifier commands.

## Delegation

The main Agent takes the guard-returned `contract_ref` (`contract_id`, version,
digest, and milestone), adds only admitted work-item IDs, gates it against the
current returned status, then writes that object to the resolved subagent inbox
through the public Loop Memory interface.

Before acting, the subagent must re-enter with its actual Agent ID, read its
returned inbox and current status, and run `delegation` again. It proceeds only
when the reference still matches. A parent prompt, remembered path, or chat
summary is never an authoritative substitute. Apply `governing-subagents`
separately when available and required; contract inheritance does not depend on
that skill.

## Bounded Correction

The guard may remove unrelated `unstarted` work, permit deferral of a fully
classified ordinary finding, stop fan-out at an admitted budget, require a
handoff, or block stale artifacts. It may not alter objectives, authority,
facts, assumptions, invariants, acceptance, expected answers, started work,
evidence, or protected risks. Persist only the returned corrected contract.

## Workflow Adapters

The default flow is the main Agent plus Loop Memory and the guard. It does not
require Superpowers. Superpowers is an optional adapter: approved designs,
plans, reviews, or verification may be referenced as generic artifacts and
events, but unavailable adapters do not weaken or block core governance.

## Completion

Before claiming completion, run the smallest fresh implementation verification
that proves the result, satisfy every contract acceptance item, pass the
`completion` gate, publish the admitted status, and leave a resumable handoff
when the broader Loop lifecycle requires one.
