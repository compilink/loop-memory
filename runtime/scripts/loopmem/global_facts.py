"""Canonical global fact index and long-memory layout primitives."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
from pathlib import Path
import re

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.paths import assert_loop_path
from scripts.loopmem.storage import (
    FileLease,
    ensure_directory,
    read_json,
    write_json_atomic,
    write_text_atomic,
    write_text_atomic_if_unchanged,
)


FACTS_RELATIVE = Path("global/facts")
FACT_INDEX_RELATIVE = FACTS_RELATIVE / "index.md"
FACT_ENTRIES_RELATIVE = FACTS_RELATIVE / "entries"
FACT_HISTORY_RELATIVE = FACTS_RELATIVE / "history"
FACT_RECEIPTS_RELATIVE = FACTS_RELATIVE / "receipts"
FACT_INDEX_POINTER = "- `~/loop-memory/global/facts/index.md`"
FACT_INDEX_TEMPLATE = "# Global Fact Index\n\n## Entries\n"
LONG_TEMPLATE = (
    "# Global Long-Term Memory\n\n"
    "## Methodology\n\n"
    "## Fact Index\n\n"
    f"{FACT_INDEX_POINTER}\n"
)
_FACT_FIRST_LINE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\]"
    r"\[(verified|superseded)\] (\S.*)$"
)


def _contained(root: Path, relative: Path) -> Path:
    return assert_loop_path(root, root / relative)


def ensure_facts_layout(loop_root: Path) -> None:
    root = Path(loop_root).resolve(strict=False)
    ensure_directory(root)
    global_dir = _contained(root, Path("global"))
    facts_dir = _contained(root, FACTS_RELATIVE)
    ensure_directory(global_dir)
    ensure_directory(facts_dir)
    for relative in (
        FACT_ENTRIES_RELATIVE,
        FACT_HISTORY_RELATIVE,
        FACT_RECEIPTS_RELATIVE,
    ):
        ensure_directory(_contained(root, relative))
    index = _contained(root, FACT_INDEX_RELATIVE)
    if not index.exists():
        write_text_atomic(index, FACT_INDEX_TEMPLATE)
    elif index.is_symlink() or not index.is_file():
        raise LoopMemoryError(
            code="global_fact_index_invalid",
            message="Global fact index must be a regular file",
            recoverable=False,
        )


def validate_long_document(content: str) -> None:
    if not isinstance(content, str):
        raise LoopMemoryError(
            code="global_long_not_canonical",
            message="Global long memory must be UTF-8 text",
            recoverable=False,
        )
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if lines[:1] != ["# Global Long-Term Memory"]:
        raise _not_canonical()
    headings = [line for line in lines if line.startswith("## ")]
    if headings != ["## Methodology", "## Fact Index"]:
        raise _not_canonical()
    if normalized.count(FACT_INDEX_POINTER) != 1:
        raise _not_canonical()
    pointer_index = lines.index(FACT_INDEX_POINTER)
    if any(line.startswith("- ") for line in lines[pointer_index + 1 :]):
        raise _not_canonical()


def validate_fact_index(loop_root: Path) -> None:
    root = Path(loop_root).resolve(strict=False)
    index = _contained(root, FACT_INDEX_RELATIVE)
    if index.is_symlink() or not index.is_file():
        raise LoopMemoryError(
            code="global_fact_index_invalid",
            message="Global fact index must be a regular file",
            recoverable=False,
        )
    lines = index.read_text(encoding="utf-8").splitlines()
    if lines[:3] != ["# Global Fact Index", "", "## Entries"]:
        raise LoopMemoryError(
            code="global_fact_index_invalid",
            message="Global fact index is not canonical",
            recoverable=False,
        )
    starts = [i for i, line in enumerate(lines[3:], start=3) if line.startswith("- ")]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        while block and not block[-1]:
            block.pop()
        match = _FACT_FIRST_LINE.fullmatch(block[0])
        if match is None or len(block) != 3:
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact index entry is malformed",
                recoverable=False,
            )
        detail_match = re.fullmatch(
            r"  Detail: ~/loop-memory/global/facts/entries/(f-[0-9a-f]{64}\.md)",
            block[1],
        )
        digest_match = re.fullmatch(r"  Content-SHA256: ([0-9a-f]{64})", block[2])
        if detail_match is None or digest_match is None:
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact index locator is malformed",
                recoverable=False,
            )
        detail = _contained(root, FACT_ENTRIES_RELATIVE / detail_match.group(1))
        if detail.is_symlink() or not detail.is_file():
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact detail is missing or unsafe",
                recoverable=False,
            )
        content = detail.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != digest_match.group(1) or digest != detail_match.group(1)[2:-3]:
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact detail digest does not match its locator",
                recoverable=False,
            )
        if content.decode("utf-8").splitlines()[0] != block[0]:
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact summary does not match its detail",
                recoverable=False,
            )


def _not_canonical() -> LoopMemoryError:
    return LoopMemoryError(
        code="global_long_not_canonical",
        message="Global long memory is not in canonical methodology/index form",
        recoverable=True,
    )


def promote_fact(loop_root: Path, entry: str) -> dict[str, object]:
    """Write one verified global fact and its summary locator atomically."""
    from scripts.loopmem import sessions

    normalized, status = sessions._normalize_entry(entry)
    if status == "inferred":
        raise LoopMemoryError(
            code="inferred_not_durable",
            message="Inferred entries cannot be promoted to global facts",
        )
    first_line = normalized.splitlines()[0]
    match = _FACT_FIRST_LINE.fullmatch(first_line)
    if match is None:
        raise LoopMemoryError(
            code="invalid_entry",
            message="Global fact entry must be verified or superseded",
        )

    root = Path(loop_root).resolve(strict=False)
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    detail_relative = FACT_ENTRIES_RELATIVE / f"f-{content_hash}.md"
    detail = _contained(root, detail_relative)
    index = _contained(root, FACT_INDEX_RELATIVE)
    locator = f"~/loop-memory/{detail_relative.as_posix()}"
    index_entry = (
        f"- [{match.group(1)}][{match.group(2)}] {match.group(3)}\n"
        f"  Detail: {locator}\n"
        f"  Content-SHA256: {content_hash}\n"
    )

    ensure_facts_layout(root)
    ensure_directory(_contained(root, Path("locks")))
    lease = _contained(root, Path("locks/promote-global-facts.lock"))
    with FileLease(lease, owner="promote:global-fact"):
        if detail.exists():
            if detail.is_symlink() or not detail.is_file():
                raise LoopMemoryError(
                    code="global_fact_conflict",
                    message="Global fact detail path is not a regular file",
                    recoverable=False,
                )
            if detail.read_bytes() != normalized.encode("utf-8"):
                raise LoopMemoryError(
                    code="global_fact_conflict",
                    message="Global fact detail content conflicts with its ID",
                    recoverable=False,
                )
        else:
            write_text_atomic(detail, normalized)

        if index.is_symlink() or not index.is_file():
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact index must be a regular file",
                recoverable=False,
            )
        index_text = index.read_text(encoding="utf-8")
        if not index_text.startswith(FACT_INDEX_TEMPLATE):
            raise LoopMemoryError(
                code="global_fact_index_invalid",
                message="Global fact index is not canonical",
                recoverable=False,
            )
        if locator in index_text:
            return {"changed": False, "path": str(detail), "index": str(index)}
        if not index_text.endswith("\n"):
            index_text += "\n"
        if not index_text.endswith("\n\n"):
            index_text += "\n"
        write_text_atomic(index, index_text + index_entry)
        return {"changed": True, "path": str(detail), "index": str(index)}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def organize_global_long(
    loop_root: Path,
    methodology_text: str,
) -> dict[str, object]:
    """Archive the exact prior long file and publish canonical methodology."""
    validate_long_document(methodology_text)
    root = Path(loop_root).resolve(strict=False)
    ensure_facts_layout(root)
    locks = _contained(root, Path("locks"))
    ensure_directory(locks)
    long_path = _contained(root, Path("global/long.md"))
    if long_path.is_symlink() or not long_path.is_file():
        raise LoopMemoryError(
            code="global_long_invalid",
            message="Global long memory must be a regular file",
            recoverable=False,
        )
    proposed = methodology_text.encode("utf-8")

    # Match migration's lock order: migration first, then cooperative writers.
    lease_specs = (
        ("migration.lock", "global-organize:migration"),
        ("promote-global-facts.lock", "global-organize:facts"),
        ("promote-global-long.lock", "global-organize:long"),
    )
    with ExitStack() as stack:
        for name, owner in lease_specs:
            stack.enter_context(FileLease(_contained(root, Path("locks") / name), owner))
        current = long_path.read_bytes()
        if current == proposed:
            existing = _receipt_for_result(root, sha256_text(methodology_text))
            return {
                "changed": False,
                "path": str(long_path),
                "history": existing["history"] if existing else None,
                "receipt": existing["receipt"] if existing else None,
            }
        try:
            current_text = current.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LoopMemoryError(
                code="global_long_invalid",
                message="Global long memory must contain UTF-8 text",
                recoverable=False,
            ) from error

        previous_hash = hashlib.sha256(current).hexdigest()
        history_relative = FACT_HISTORY_RELATIVE / f"long-{previous_hash}.md"
        receipt_relative = FACT_RECEIPTS_RELATIVE / f"o-{previous_hash}.json"
        history = _contained(root, history_relative)
        receipt = _contained(root, receipt_relative)
        _write_immutable_text(history, current_text, "global_history_conflict")

        receipt_value: dict[str, object] = {
            "schema_version": 1,
            "organization_id": f"o-{previous_hash}",
            "previous_long_sha256": previous_hash,
            "history_path": history_relative.as_posix(),
            "history_sha256": previous_hash,
            "entry_sha256": _entry_hashes(current_text),
            "resulting_long_sha256": hashlib.sha256(proposed).hexdigest(),
        }
        _write_immutable_json(receipt, receipt_value)

        if not write_text_atomic_if_unchanged(
            long_path,
            methodology_text,
            current,
        ):
            raise LoopMemoryError(
                code="global_long_conflict",
                message="Global long memory changed during organization",
                recoverable=True,
            )
        try:
            validate_long_document(long_path.read_text(encoding="utf-8"))
            if history.read_bytes() != current or read_json(receipt) != receipt_value:
                raise LoopMemoryError(
                    code="global_organization_invalid",
                    message="Global organization evidence failed verification",
                    recoverable=False,
                )
        except BaseException:
            rolled_back = write_text_atomic_if_unchanged(
                long_path,
                current_text,
                proposed,
            )
            if not rolled_back:
                raise LoopMemoryError(
                    code="global_organization_ambiguous",
                    message="Global long memory changed before rollback",
                    recoverable=False,
                )
            raise
        return {
            "changed": True,
            "path": str(long_path),
            "history": str(history),
            "receipt": str(receipt),
        }


def _write_immutable_text(path: Path, value: str, code: str) -> None:
    expected = value.encode("utf-8")
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise LoopMemoryError(
                code=code,
                message="Immutable global memory evidence conflicts",
                recoverable=False,
            )
        return
    write_text_atomic(path, value)


def _write_immutable_json(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or read_json(path) != value:
            raise LoopMemoryError(
                code="global_receipt_conflict",
                message="Global organization receipt conflicts",
                recoverable=False,
            )
        return
    write_json_atomic(path, value)


def _entry_hashes(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("- ")]
    result: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        for index in range(start + 1, end):
            if lines[index].startswith("## "):
                end = index
                break
        block = "".join(lines[start:end]).strip("\r\n")
        if block:
            result.append(hashlib.sha256(block.encode("utf-8")).hexdigest())
    return sorted(set(result))


def _receipt_for_result(root: Path, result_hash: str) -> dict[str, str] | None:
    receipts = _contained(root, FACT_RECEIPTS_RELATIVE)
    for path in sorted(receipts.glob("o-*.json")):
        if path.is_symlink() or not path.is_file():
            raise LoopMemoryError(
                code="global_receipt_conflict",
                message="Global organization receipt path is unsafe",
                recoverable=False,
            )
        value = read_json(path)
        if value.get("resulting_long_sha256") != result_hash:
            continue
        history_relative = value.get("history_path")
        if not isinstance(history_relative, str):
            raise LoopMemoryError(
                code="global_receipt_conflict",
                message="Global organization receipt is incomplete",
                recoverable=False,
            )
        history = _contained(root, Path(history_relative))
        if hashlib.sha256(history.read_bytes()).hexdigest() != value.get("history_sha256"):
            raise LoopMemoryError(
                code="global_receipt_conflict",
                message="Global organization history digest changed",
                recoverable=False,
            )
        return {"receipt": str(path), "history": str(history)}
    return None


def verify_receipt_coverage(
    loop_root: Path,
    required_hashes: set[str],
) -> bool:
    """Return whether an organization receipt preserves every required block."""
    if not required_hashes:
        return True
    root = Path(loop_root).resolve(strict=False)
    receipts = _contained(root, FACT_RECEIPTS_RELATIVE)
    for path in sorted(receipts.glob("o-*.json")):
        if path.is_symlink() or not path.is_file():
            raise LoopMemoryError(
                code="global_receipt_invalid",
                message="Global organization receipt path is unsafe",
                recoverable=False,
            )
        value = read_json(path)
        previous = value.get("previous_long_sha256")
        history_relative = value.get("history_path")
        entry_hashes = value.get("entry_sha256")
        if (
            value.get("schema_version") != 1
            or not isinstance(previous, str)
            or not re.fullmatch(r"[0-9a-f]{64}", previous)
            or value.get("organization_id") != f"o-{previous}"
            or history_relative != f"global/facts/history/long-{previous}.md"
            or not isinstance(entry_hashes, list)
            or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in entry_hashes)
        ):
            raise LoopMemoryError(
                code="global_receipt_invalid",
                message="Global organization receipt fields are invalid",
                recoverable=False,
            )
        history = _contained(root, Path(history_relative))
        if history.is_symlink() or not history.is_file():
            raise LoopMemoryError(
                code="global_receipt_invalid",
                message="Global organization history is missing or unsafe",
                recoverable=False,
            )
        history_bytes = history.read_bytes()
        if (
            hashlib.sha256(history_bytes).hexdigest() != value.get("history_sha256")
            or hashlib.sha256(history_bytes).hexdigest() != previous
            or set(entry_hashes) != set(_entry_hashes(history_bytes.decode("utf-8")))
        ):
            raise LoopMemoryError(
                code="global_receipt_invalid",
                message="Global organization history or entry hashes changed",
                recoverable=False,
            )
        if required_hashes.issubset(set(entry_hashes)):
            return True
    return False
