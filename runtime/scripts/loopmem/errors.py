from dataclasses import dataclass, field


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_CORRUPT = 4


@dataclass(frozen=True)
class LoopMemoryError(Exception):
    code: str
    message: str
    recoverable: bool = True

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __reduce__(self):
        return type(self), (self.code, self.message, self.recoverable)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True)
class RequiredAccess:
    path: str = "~/loop-memory"
    read: bool = True
    write: bool = True
    execute: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "read": self.read,
            "write": self.write,
            "execute": self.execute,
        }


@dataclass(frozen=True)
class AccessDenied(LoopMemoryError):
    code: str = field(default="environment_access_denied", init=False)
    message: str = field(
        default="The environment denied access required by Loop memory",
        init=False,
    )
    recoverable: bool = field(default=True, init=False)
    required_access: RequiredAccess = field(default_factory=RequiredAccess)
    next_action: str = field(default="request_environment_access", init=False)

    def __reduce__(self):
        return type(self), ()
