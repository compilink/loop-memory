# Task Contract Reference

The task contract is canonical JSON stored in the current Loop Memory session
status through the public `session-write` command. The guard accepts schema
version `1` and writes JSON with sorted keys, compact separators, and one final
newline.

## Contract

```json
{
  "schema_version": 1,
  "contract_id": "tc-example",
  "version": 1,
  "previous_digest": null,
  "state": "discovery",
  "objective": "A directly verifiable objective",
  "milestone": "The current smallest useful milestone",
  "constraints": [{"id": "C-001", "source": "AGENTS.md#rule"}],
  "milestone_constraint_ids": ["C-001"],
  "facts": [{"id": "F-001", "statement": "Verified fact", "evidence": "path:line"}],
  "assumptions": [{"id": "A-001", "statement": "Open assumption", "status": "open"}],
  "invariants": [{"id": "I-001", "statement": "Must remain true", "verification": "command or evidence"}],
  "acceptance": [{"id": "AC-001", "statement": "Observable result", "verification": "command", "status": "pending"}],
  "scope": {"allowed": ["Current milestone work"], "forbidden": ["Explicitly deferred work"]},
  "decision": {
    "selected_path": "The minimal sufficient path",
    "preserves": ["I-001"],
    "simplifications": [
      {"id": "S-001", "summary": "Accepted limit", "ceiling": "Current upper bound", "trigger": "Evidence that requires reconsideration"}
    ]
  },
  "work_items": [{"id": "W-001", "status": "unstarted", "constraint_ids": ["C-001"]}],
  "budget": {"max_open_agents": 8, "max_cumulative_agents": 45},
  "usage": {"open_agents": 0, "cumulative_agents": 0},
  "findings": [],
  "artifacts": [],
  "evidence": [],
  "progress": {"phase": "discovery", "next_action": "Next admitted action"}
}
```

`state` is `discovery` or `execution`. Acceptance status is `pending` or
`satisfied`; work-item status is `unstarted`, `in-progress`, `completed`, or
`deferred`. Every fact needs evidence. An unresolved assumption uses `open`;
remove or promote it in a later version when resolved.

`decision.preserves` must name every invariant. `simplifications` may be empty.
Each entry is only decision metadata and requires a unique ID, summary, current
ceiling, and evidence-based trigger. It does not schedule work or authorize
automatic cleanup.

Findings use `risk_category` `security`, `data-integrity`, `compatibility`, or
`ordinary`, plus disposition `block`, `fix-now`, or `defer`. The first three
categories cannot be deferred. Artifact entries contain `kind`, `path`,
`sha256`, and optional `adapter`. Relative paths resolve from the guard's cwd.
Fresh completion evidence has `kind=implementation-verification`, a statement,
verification locator, and `fresh=true`.

## Events

Contract events use a complete candidate contract: `task-start`,
`execution-contract`, `execution-proposal`, `review-disposition`,
`milestone-transition`, and `completion`. After task start, a candidate version
must equal the current version plus one and `previous_digest` must equal the
canonical SHA-256 digest of the current contract.

`delegation` is read-only. Its candidate is the exact reference returned by the
last successful contract event plus admitted work-item IDs:

```json
{
  "contract_ref": {
    "contract_id": "tc-example",
    "version": 1,
    "digest": "canonical-sha256",
    "milestone": "The current smallest useful milestone"
  },
  "work_item_ids": ["W-001"]
}
```

The main Agent gates this object before dispatch and writes it to the resolved
subagent inbox. The subagent re-enters Loop Memory and gates the same object
against the returned current status before acting.

## Authority Index

An optional project index maps stable constraint IDs to authoritative sources:

```toml
[constraints."C-001"]
source = "AGENTS.md#rule"
```

The guard verifies the mapping but never runs a referenced verifier command.
