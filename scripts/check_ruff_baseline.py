"""Compare a complete Ruff JSON report with the reviewed diagnostic baseline."""

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "backend/ruff-baseline.json"
ALLOWED_PREFIXES = (
    "backend/app/",
    "backend/entry/",
    "backend/runtime_controller/",
    "backend/runtime_common/",
)
ALLOWED_CODES = frozenset({"BLE001", "N818", "S603", "S104", "S108", "PLR0913"})
N818_NAMES = frozenset(
    {
        "RetryTaskLater",
        "ControllerNotFound",
        "ControllerGenerationMissing",
        "MissingRuntime",
        "MissingGenerationCandidate",
        "PolicyViolation",
        "SocketLimit",
    },
)
DiagnosticKey = tuple[str, str, int, int, int, int, str]
MCP_LIST_FILES_DIAGNOSTIC: DiagnosticKey = (
    "backend/app/mcp/server.py",
    "PLR0913",
    220,
    11,
    220,
    21,
    "Too many arguments in function definition (6 > 5)",
)


class SchemaError(ValueError):
    """The report or baseline cannot be compared safely."""


def _fail(message: str, cause: BaseException | None = None) -> NoReturn:
    """Raise a comparison-schema failure, preserving any parse cause."""
    if cause is not None:
        raise SchemaError(message) from cause
    raise SchemaError(message)


def _write(line: str) -> None:
    """Write one complete output line to stdout."""
    sys.stdout.write(line + "\n")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return cast("dict[str, object]", value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _relative_path(value: object) -> str:
    raw = Path(_string(value, "path/filename"))
    if ".." in raw.parts:
        _fail(f"path contains a parent traversal: {raw}")
    try:
        resolved = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
        relative = resolved.relative_to(ROOT)
    except (OSError, ValueError) as exc:
        _fail(f"path escapes the repository: {raw}", exc)
    path = relative.as_posix()
    if not path.startswith(ALLOWED_PREFIXES):
        _fail(f"path is outside the checked production trees: {path}")
    return path


def _position(value: object, label: str) -> tuple[int, int]:
    location = _mapping(value, label)
    return (
        _positive(location.get("row"), f"{label}.row"),
        _positive(location.get("column"), f"{label}.column"),
    )


def _validate_code(policy: DiagnosticKey) -> None:
    path, code, _row, _column, _end_row, _end_column, message = policy
    if code not in ALLOWED_CODES:
        _fail(f"diagnostic code is not eligible for the baseline: {code}")
    if code == "PLR0913" and policy != MCP_LIST_FILES_DIAGNOSTIC:
        _fail("PLR0913 entry is outside the approved MCP tool")
    if code == "N818" and message not in {
        f"Exception name `{name}` should be named with an Error suffix"
        for name in N818_NAMES
    }:
        _fail(f"N818 entry names an unapproved exception: {message}")
    if code == "S603" and (
        path != "backend/app/infra/processes.py" or "subprocess" not in message
    ):
        _fail(f"S603 entry is outside the approved converter boundary: {path}")
    if code == "S104":
        approved = {
            "backend/entry/api.py",
            "backend/entry/llm_gateway.py",
            "backend/entry/mcp_run.py",
            "backend/runtime_controller/policy.py",
        }
        if path not in approved or message != "Possible binding to all interfaces":
            _fail(f"S104 entry is outside the approved listeners: {path}")
    if code == "S108" and (
        path != "backend/runtime_controller/policy.py"
        or not message.startswith(
            'Probable insecure usage of temporary file or directory: "/tmp',
        )
    ):
        _fail(f"S108 entry is outside fixed container tmpfs: {path}")


def _key(values: DiagnosticKey) -> DiagnosticKey:
    """Validate and return an exact diagnostic identity."""
    _validate_code(values)
    return values


def load_report(path: Path) -> list[tuple[DiagnosticKey, dict[str, object]]]:
    """Load and validate every diagnostic from a complete Ruff JSON report."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"report is unavailable: {path}", exc)
    except json.JSONDecodeError as exc:
        _fail(f"report is not valid JSON: {path}", exc)
    if not isinstance(raw, list):
        _fail("report must be a JSON list")
    diagnostics: list[tuple[DiagnosticKey, dict[str, object]]] = []
    seen: set[DiagnosticKey] = set()
    for index, value in enumerate(cast("list[object]", raw)):
        item = _mapping(value, f"report[{index}]")
        path_value = _relative_path(item.get("filename"))
        code = _string(item.get("code"), f"report[{index}].code")
        message = _string(item.get("message"), f"report[{index}].message")
        row, column = _position(item.get("location"), f"report[{index}].location")
        end_row, end_column = _position(
            item.get("end_location"),
            f"report[{index}].end_location",
        )
        if (end_row, end_column) < (row, column):
            _fail(f"report[{index}] has an invalid range")
        key = (path_value, code, row, column, end_row, end_column, message)
        if key in seen:
            _fail(f"report contains a duplicate diagnostic: {key}")
        seen.add(key)
        diagnostics.append((key, item))
    return diagnostics


def load_baseline(path: Path) -> dict[DiagnosticKey, dict[str, object]]:
    """Load and validate the reviewed baseline entries."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"baseline is unavailable: {path}", exc)
    except json.JSONDecodeError as exc:
        _fail(f"baseline is not valid JSON: {path}", exc)
    if not isinstance(raw, list):
        _fail("baseline must be a JSON list")
    entries: dict[DiagnosticKey, dict[str, object]] = {}
    for index, value in enumerate(cast("list[object]", raw)):
        item = _mapping(value, f"baseline[{index}]")
        reason = _string(item.get("reason"), f"baseline[{index}].reason").strip()
        evidence = _string(
            item.get("evidence"),
            f"baseline[{index}].evidence",
        ).strip()
        if not reason or not evidence:
            _fail(f"baseline[{index}] needs reason and evidence")
        baseline_path = Path(_string(item.get("path"), f"baseline[{index}].path"))
        if baseline_path.is_absolute():
            _fail(f"baseline[{index}].path must be repository-relative")
        path_value = _relative_path(str(baseline_path))
        code = _string(item.get("code"), f"baseline[{index}].code")
        message = _string(item.get("message"), f"baseline[{index}].message")
        row = _positive(item.get("row"), f"baseline[{index}].row")
        column = _positive(item.get("column"), f"baseline[{index}].column")
        end_row = _positive(item.get("end_row"), f"baseline[{index}].end_row")
        end_column = _positive(item.get("end_column"), f"baseline[{index}].end_column")
        if (end_row, end_column) < (row, column):
            _fail(f"baseline[{index}] has an invalid range")
        key = _key((path_value, code, row, column, end_row, end_column, message))
        if key in entries:
            _fail(f"baseline contains a duplicate diagnostic: {key}")
        entries[key] = item
    return entries


def _format(key: DiagnosticKey) -> str:
    path, code, row, column, _end_row, _end_column, message = key
    return f"{path}:{row}:{column} {code} {message}"


def compare(
    report: list[tuple[DiagnosticKey, dict[str, object]]],
    baseline: dict[DiagnosticKey, dict[str, object]],
) -> int:
    """Print the full report and fail on any new or stale diagnostic."""
    report_keys = {key for key, _item in report}
    baseline_keys = set(baseline)
    new_keys = sorted(report_keys - baseline_keys)
    stale_keys = sorted(baseline_keys - report_keys)

    _write(f"Ruff diagnostics: {len(report)} (full report follows)")
    for key, _item in report:
        if key in baseline:
            reason = _string(baseline[key].get("reason"), "reason")
            _write(f"CONFIRMED {_format(key)} | {reason}")
        else:
            _write(f"NEW {_format(key)}")
    for key in stale_keys:
        _write(f"STALE {_format(key)}")
    _write(
        f"confirmed={len(report_keys & baseline_keys)} "
        f"new={len(new_keys)} stale={len(stale_keys)}",
    )
    return 0 if not new_keys and not stale_keys else 1


def main() -> int:
    """Run the baseline comparison CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    try:
        report = load_report(args.report)
        baseline = load_baseline(args.baseline)
    except SchemaError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return compare(report, baseline)


if __name__ == "__main__":
    raise SystemExit(main())
