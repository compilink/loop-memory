"""Small immutable capability and notice values returned by enter."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    global_read: bool = True
    global_promote: bool = True
    project_read: bool = True
    project_promote: bool = True
    session_read: bool = True
    session_write: bool = True
    session_close: bool = True
    migration_apply: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "global_read": self.global_read,
            "global_promote": self.global_promote,
            "project_read": self.project_read,
            "project_promote": self.project_promote,
            "session_read": self.session_read,
            "session_write": self.session_write,
            "session_close": self.session_close,
            "migration_apply": self.migration_apply,
        }


@dataclass(frozen=True)
class Notice:
    code: str
    scope: str
    blocking: tuple[str, ...] = ()
    next_action: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "scope": self.scope,
            "blocking": list(self.blocking),
        }
        if self.next_action is not None:
            result["next_action"] = self.next_action
        return result
