# Loop Memory Operations

## Contents

- [Invocation and authority](#invocation-and-authority)
- [Enter and access recovery](#enter-and-access-recovery)
- [Capabilities and paths](#capabilities-and-paths)
- [Session writes and promotion](#session-writes-and-promotion)
- [Lifecycle and ownership](#lifecycle-and-ownership)
- [External legacy and migration](#external-legacy-and-migration)
- [Diagnosis and maintenance](#diagnosis-and-maintenance)
- [JSON and exit codes](#json-and-exit-codes)

## Invocation and Authority

Use the installed `loop-memory` launcher. During controlled development, invoke
`scripts/loop_memory.py` by absolute path. Every operation uses `--json` and
emits one JSON object.

```bash
loop-memory COMMAND ... --json
loop-memory --json --help
loop-memory COMMAND --help --json
```

The only canonical data root is `~/loop-memory`. `--root` exists only for an
explicitly authorized isolated test root; it is never a project fallback.
Product-memory locations are opaque and cannot be selected as Loop roots,
sources, or destinations.

The old `~/.codex/loop-memory` directory is recognized only as the one-time
legacy relocation source when `enter` transactionally establishes the new
authority. Do not copy, rename, link, or edit either root manually. A valid
old and new root together return a typed conflict instead of being merged.

## Enter and Access Recovery

The installer configures Codex's native `loop-memory` permission profile as
the default for new threads. It extends `:workspace` and grants the exact
`~/loop-memory` filesystem entry with `write` access (which includes reads),
while preserving the existing workspace network setting. Existing threads keep
the sandbox snapshot created when they started; close and recreate a thread if
it predates the profile installation.

```bash
loop-memory enter \
  --cwd PATH \
  --session-id ID \
  [--project-root PATH] \
  [--agent-id ID] \
  --json
```

`--cwd` is the actual existing directory. `--project-root` is optional and is
authoritative when the host knows the workspace root; otherwise the canonical
cwd is the project identity. `--session-id` is the host task/session ID. The
main Agent omits `--agent-id`; a subagent supplies its own stable host ID.

`preflight` and `initialize` are compatibility aliases for `enter`; new
workflows use `enter`. Call it before ordinary Loop operations, at task start,
after resume or compaction, after handoff, after cwd/project changes, before
the first memory write, and before close. Never trust paths remembered from
chat or a previous entry result after those transitions.

When the registry has exactly one newest active generation whose active tree
and all archive locations are absent, `enter` recreates that same session's
empty working tree under the registry lease. It returns
`session_recovered=true` and the non-blocking
`session_memory_reinitialized` notice once; the next `enter` is quiet and
idempotent. This is loss-tolerant recovery of already missing working state,
not history reconstruction. Any active/archive conflict, duplicate archive,
unsafe path, or broken predecessor chain remains an `ambiguous_session` block.

Access denial has this body-free shape:

```json
{
  "ok": false,
  "error": {
    "code": "environment_access_denied",
    "recoverable": true
  },
  "required_access": {
    "path": "~/loop-memory",
    "read": true,
    "write": true,
    "execute": false
  },
  "next_action": "request_environment_access"
}
```

When the host supports access requests, request exactly the returned path and
modes and retry `enter` once. Reject a changed path or broader request. If the
request or retry fails, report the typed block; do not use elevated privilege,
broaden access to `~/`, retry repeatedly, or create a fallback. While the
`enter` result remains blocked, do not write Loop Memory, promote, migrate, or
take irreversible external side effects. Read-only diagnosis and recoverable
local work may continue, but the Agent must not treat the current Loop state as
trusted until `enter` succeeds.

## Capabilities and Paths

A successful `enter` returns `root`, `project_id`, `session_id`,
`session_generation`, `resumes_from`, `resume_handoff`, `degraded`,
`capabilities`, `notices`, and `paths`. Read only paths paired with a true
capability:

| Capability | Permitted action |
| --- | --- |
| `global_read` | Read returned global context. |
| `global_promote` | Promote verified reusable method. |
| `project_read` | Read returned project context. |
| `project_promote` | Promote verified project knowledge. |
| `session_read` | Read status, handoff, inbox, or outbox as owned. |
| `session_write` | Write live session or actor state. |
| `session_close` | Close after all outboxes are resolved. |
| `migration_apply` | Apply the specifically returned migration action. |

`degraded=true` is usable. Each notice names its `scope`, blocked capabilities,
and safe next action. Continue only with true capabilities; a local external
legacy issue or unresolved outbox must not disable unrelated global, project,
or session work. An access, root, identity, containment, ownership, lock, or
integrity failure returns `ok=false` and stops the affected operation.
An `other-project` notice is diagnostic only for the current task and must not
disable capabilities that the returned current scope marks true.

The returned project paths include `project_long`, `project_medium`, and
`project_short` for the explicit project horizons. `project_memory` remains the
backward-compatible aggregate path for older data. Horizon files are created
when first used; a missing horizon is an empty scope.

## Session Writes and Promotion

Supply bodies through regular non-symlink UTF-8 files:

```bash
loop-memory session-write \
  --cwd PATH --thread-id ID \
  --kind status|handoff|inbox|outbox \
  --input FILE [--agent-id ID] --json

loop-memory promote \
  --cwd PATH --thread-id ID \
  --scope project|project-long|project-medium|project-short|global-long|global-medium|global-fact \
  --section NAME --input FILE --json
```

`global-fact` is the durable-fact path. The input must be a dated
`[verified]` or `[superseded]` entry; inferred entries are rejected. The
runtime content-addresses the complete body at
`~/loop-memory/global/facts/entries/f-<sha256>.md` and appends only its summary,
detail locator, and digest to `global/facts/index.md`. Repeating the same
promotion is idempotent. The existing `global-long` scope remains for
methodology changes and must use the `Methodology` section.

Each `session-write`, `session-close`, and `promote` call re-enters and checks
its exact required capability. Legacy staging, migration, diagnosis, and
maintenance follow their own returned next-action and integrity contracts;
they are not described as a single identity gate. Verify exit `0`, `ok=true`,
the returned identity, and the direct result (`path` or `changed`).
Project-long/global-long entries are evidence-backed `verified` or
`superseded` knowledge; inference belongs in global-medium or the current
outbox. Never include secrets, credentials, raw customer data, or unnecessary
personal data.
The compatibility `global/short.md` layer remains readable for migrated data
but is deprecated for new writes; current short-lived state belongs in the
active session.

Session writes are bounded to 16 KiB, reject whitespace-only and first-use
template-only bodies, and are a byte-for-byte no-op when unchanged. The runtime
may create the active session directory during `enter`, but it creates status,
handoff, inbox, and outbox files only after a meaningful write.
An absent outbox file therefore means no candidate; malformed agent directories
still block close or cleanup.

## Global long-memory organization

`~/loop-memory/global/long.md` is read on every substantive task and is kept
small by invariant: its only sections are `Methodology` and `Fact Index`, with
the latter pointing to `~/loop-memory/global/facts/index.md`. Complete global
facts live in content-addressed detail files; the index provides their short
summaries and exact paths. Prior long-file versions are preserved under
`global/facts/history/` with immutable organization receipts under
`global/facts/receipts/`.

Every `enter` validates this shape and the index. A legacy or overfull long
file does not block unrelated reads or session work: it returns the
non-blocking `global_long_organization_due` notice with
`next_action=global-organize`. In the same task, prepare a canonical
methodology file and run:

```bash
loop-memory global-organize \
  --cwd PATH --thread-id ID --methodology FILE --json
```

The command re-enters, requires `global_promote`, archives the exact previous
`global/long.md`, writes the canonical replacement with a compare-and-swap,
and verifies the history and receipt. It is deterministic per task and does
not rely on a periodic maintenance job or on manual edits to Loop internals.

## Lifecycle and Ownership

- `status` is current resumable state. `handoff` is a snapshot written for
  compaction, transfer, or close; it does not replace status.
- Main owns shared status, handoff, promotion, and subagent inboxes. A subagent
  reads its returned inbox and writes only its returned outbox. Main verifies,
  accepts or rejects candidates. Each actor clears only its own resolved
  outbox.
- Close only when `session_close=true` and every outbox is resolved:

```bash
loop-memory session-close --cwd PATH --thread-id ID --json
```

If an archived host session resumes, `enter` creates a new generation and
returns `resumes_from` plus the prior handoff candidate. The archived
generation remains unchanged. Use only the newly returned active paths.

## External Legacy and Migration

External legacy sources are read-only. Discovery may inventory metadata and
`legacy-stage` may copy verified bytes into an internal snapshot, but the
source remains byte-for-byte and identity unchanged. Credentials make a
snapshot non-importable; ambiguity or conflict degrades only the capabilities
named by `enter`.

```bash
loop-memory legacy-stage --cwd PATH --json
loop-memory migrate-scan --cwd PATH [--legacy-path PATH ...] --json
loop-memory migrate-refresh --manifest FILE --json
loop-memory migrate-apply --manifest FILE --classification FILE --json
```

Use these compatibility commands only when `enter` or `doctor` returns the
matching next action. Trust returned manifests and snapshot IDs; do not edit
registries, ledgers, receipts, classifications, aliases, snapshots, or
transaction files manually. A copied snapshot stays in Loop custody by
default. `legacy-delete --snapshot ID` deletes only the named internal
snapshot after an explicit user decision; it never alters the external source.

Only a deterministic, evidence-complete mapping may be applied. Suspected
credentials, ambiguous semantics, target conflicts, corrupt structure, or
contradictory facts remain capability-specific blocks. Never convert an
external source into current authority or use it as a writable fallback.
If a manifest's custody snapshot is absent, `enter` reports the same
project-scoped degradation as a missing staging snapshot and keeps session and
global capabilities available; run `legacy-stage` only when the returned
next-action is accepted.

## Diagnosis and Maintenance

```bash
loop-memory doctor --cwd PATH --json
loop-memory maintain --json
```

`enter`, not a periodic job, owns correctness and deterministic convergence on
every task. `doctor` is read-only and explains notices, required access, root
state, locks, and safe recovery without reading memory bodies. `maintain`
performs eligible internal space recovery only; it is not a correctness gate.
Neither command authorizes manual internal edits or external-source changes.

## JSON and Exit Codes

Success requires process exit `0`, `ok=true`, the expected operation, and all
operation-specific fields. Failure JSON contains a stable `error.code`, a
body-free message, `recoverable`, and only the typed next-action fields needed
for recovery. Memory bodies never appear in CLI or hook diagnostics.

| Exit | Meaning | Response |
| ---: | --- | --- |
| `0` | Successful operation or JSON help. | Verify required fields and direct result. |
| `2` | Invalid usage. | Correct arguments. |
| `3` | Recoverable typed block. | Follow only the returned next action, then re-enter. |
| `4` | Unsafe identity, authority, integrity, or internal state. | Stop writes and preserve evidence; use `doctor` when safe. |

Do not treat `recoverable` as permission to guess. A created file alone is not
proof of success; the exit code and JSON result together are authoritative.
