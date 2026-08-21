# Loop Memory shared runtime

This staged package is installed for one macOS/Linux OS user at
`~/.local/share/loop-memory`, with the thin launcher at `~/.local/bin/loop-memory`.
The data authority is `~/loop-memory`; no project directory or product-memory
directory is used as a fallback.

Every task reads `~/loop-memory/global/long.md`. That file is intentionally
small: it contains reusable methodology and the fact-index path only. Verified
global facts are promoted with `--scope global-fact`; their complete bodies are
content-addressed under `~/loop-memory/global/facts/entries/`, while
`global/facts/index.md` keeps summaries, locators, and digests. Previous long
versions are retained in the facts history with organization receipts.

The launcher resolves the shared install from the invoking user's home, so it
works from an unrelated current directory.  It emits one JSON object for CLI
operations.  Lifecycle adapters are synchronous and bounded and pass only
host identity to `enter`; memory bodies and configuration values are not
rendered into hook context or validation output.

If `enter` returns `environment_access_denied`, the adapter emits a structured
blocked context. The host may request exactly `~/loop-memory` read/write access
and retry once. Until a successful `enter`, the Agent must stop Loop Memory
writes, promotion, migration, and irreversible external side effects; read-only
diagnosis and recoverable local work may continue.

`stage_user_config.py` consumes sanitized inputs and writes disposable
`config.toml`, `hooks.json`, and `settings.json` artifacts.  The merge keeps
unknown settings and existing hook definitions, adds only the required
Loop-Memory hooks, and normalizes `~/loop-memory` exactly once.  It does not
introduce a `default_permissions` profile or widen Claude permissions.

`validate_user_config.py` parses staged TOML/JSON, compares staged top-level
key sets with the sanitized inputs, and prints only `OK`, the canonical root,
expected hook names, and `codex_trust_review=required`. That final deployment
gate means the hook definitions are structurally installed but Codex still
requires its one-time product trust review. Never point either script at a live user
configuration unless an operator has separately approved a deployment backup
and review.

Codex hook definitions require the product's one-time trust review after a
definition changes. Automated deployment must stop at
`codex_trust_review=required`; in Codex, open `/hooks`, review the displayed
commands, and approve them before declaring hook acceptance complete. Later
lifecycle events reuse that persistent trust until the definitions change
again. No deployment script may silently approve or edit product trust state.

`enter` checks the long-memory and fact-index invariants on every task. A
legacy or overfull `global/long.md` returns the non-blocking notice
`global_long_organization_due`; prepare a canonical methodology file and run
`loop-memory global-organize --cwd PATH --thread-id ID --methodology FILE --json`
in that same task. The command archives the exact old file and verifies the
replacement; no periodic scheduler is required.

If a registry-only active session has no active tree and no archive, `enter`
recreates its empty working tree in place and returns the one-time,
non-blocking `session_memory_reinitialized` notice. This discards only already
missing session working state; real trees, archives, and unsafe or conflicting
evidence continue to fail closed.

## Staged installation and recovery

Deployment is an operator-reviewed transaction. First make a sibling backup of
the existing shared launcher/runtime, affected skills, global methodology,
Codex TOML/hooks, and Claude settings. Run `stage_user_config.py` against
sanitized copies (or a separately approved deployment staging area), validate
the generated artifacts, then copy the runtime to `~/.local/share/loop-memory`,
the launcher to `~/.local/bin/loop-memory`, and the reviewed skills/config
artifacts to their user-level destinations. Validate syntax, run the launcher
smoke checks, and review `/hooks` once after hook definitions change.

If any validation or smoke check fails, restore the sibling backup as one
operator-controlled rollback and rerun validation. Do not hand-edit or
manually move the former `~/.codex/loop-memory` tree: after the shared runtime
is available, use its `doctor`/`diagnose` inventory and the published
relocation transaction to migrate the real root. A failed relocation is
recovered through the CLI's transaction evidence, never by `mv`, symlink, or
metadata edits.

The concrete operator sequence is:

```text
1. Create a sibling backup containing the current shared runtime and launcher,
   affected skills, global AGENTS.md, Codex config/hooks, and Claude settings.
2. Stage sanitized config inputs with stage_user_config.py, then run
   validate_user_config.py and inspect only its OK/root/hook summary. Treat
   codex_trust_review=required as a deployment gate, not a success claim.
3. Copy the reviewed runtime to ~/.local/share/loop-memory, launcher to
   ~/.local/bin/loop-memory, and reviewed skills/config artifacts to their
   user-level destinations.
4. Run launcher --json --help, access-check, and enter smoke checks from an
   unrelated cwd. Pause deployment at codex_trust_review=required; review and
   approve Codex /hooks once after hook definitions change, then record that
   human acceptance separately.
5. On any failure, restore the sibling backup and rerun validation. Keep the
   backup until the new enter path is accepted.
6. Migrate the old authority only through the CLI relocation transaction;
   doctor/diagnose explain recovery. Never manually mv, symlink, or edit its
   metadata.
```

## Verification

```text
python3 -m unittest tests.test_packaging -v
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m compileall -q scripts adapters bin
```
