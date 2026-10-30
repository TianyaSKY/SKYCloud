"""Behavioral checks for the reviewed Ruff diagnostic baseline gate."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-ruff-baseline.py"
S104_MESSAGE = "Possible binding to all interfaces"


def diagnostic(path="backend/entry/api.py", row=18, column=39, code="S104", message=S104_MESSAGE):
    return {
        "filename": str(ROOT / path),
        "code": code,
        "message": message,
        "location": {"row": row, "column": column},
        "end_location": {"row": row, "column": column + 13},
    }


def baseline_entry(path="backend/entry/api.py", row=18, column=39, **overrides):
    entry = {
        "path": path,
        "code": "S104",
        "row": row,
        "column": column,
        "end_row": row,
        "end_column": column + 13,
        "message": S104_MESSAGE,
        "reason": "Container public listener is required",
        "evidence": "docker-entrypoint.sh exposes the deployment listener",
    }
    entry.update(overrides)
    return entry


def run_gate(tmp_path, report, baseline):
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--baseline",
            str(baseline_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_confirmed_entry_passes_and_stays_visible(tmp_path):
    result = run_gate(tmp_path, [diagnostic()], [baseline_entry()])
    assert result.returncode == 0, result.stderr
    assert "CONFIRMED backend/entry/api.py:18:39 S104" in result.stdout
    assert "Container public listener is required" in result.stdout
    assert "confirmed=1 new=0 stale=0" in result.stdout


def test_extra_diagnostic_fails_without_count_discount(tmp_path):
    result = run_gate(
        tmp_path,
        [diagnostic(), diagnostic(path="backend/entry/mcp_run.py", row=30, column=27)],
        [baseline_entry()],
    )
    assert result.returncode == 1
    assert "NEW backend/entry/mcp_run.py:30:27 S104" in result.stdout
    assert "confirmed=1 new=1 stale=0" in result.stdout


def test_line_shift_creates_new_and_stale_diagnostics(tmp_path):
    result = run_gate(tmp_path, [diagnostic(row=19)], [baseline_entry(row=18)])
    assert result.returncode == 1
    assert "NEW backend/entry/api.py:19:39 S104" in result.stdout
    assert "STALE backend/entry/api.py:18:39 S104" in result.stdout


def test_empty_sets_pass_but_nonempty_baseline_with_empty_report_fails(tmp_path):
    empty = run_gate(tmp_path, [], [])
    assert empty.returncode == 0
    nonempty = run_gate(tmp_path, [], [baseline_entry()])
    assert nonempty.returncode == 1
    assert "STALE backend/entry/api.py:18:39 S104" in nonempty.stdout


def test_unregistered_rule_is_visible_and_fails_as_new(tmp_path):
    result = run_gate(
        tmp_path,
        [diagnostic(code="E501", message="Line too long")],
        [],
    )
    assert result.returncode == 1
    assert "NEW backend/entry/api.py:18:39 E501 Line too long" in result.stdout
    assert "confirmed=0 new=1 stale=0" in result.stdout


def test_only_approved_mcp_list_files_parameter_count_can_be_registered(tmp_path):
    path = "backend/app/mcp/server.py"
    message = "Too many arguments in function definition (6 > 5)"
    report = diagnostic(path=path, row=219, column=11, code="PLR0913", message=message)
    report["end_location"]["column"] = 21
    approved = baseline_entry(
        path=path, row=219, column=11, end_column=21,
        code="PLR0913", message=message,
    )
    result = run_gate(tmp_path, [report], [approved])
    assert result.returncode == 0, result.stderr
    assert "confirmed=1 new=0 stale=0" in result.stdout

    unapproved = {**approved, "row": 220, "end_row": 220}
    result = run_gate(tmp_path, [], [unapproved])
    assert result.returncode == 2
    assert "outside the approved MCP tool" in result.stderr


@pytest.mark.parametrize(
    "report,baseline,expected",
    [
        ([diagnostic()], [baseline_entry(), baseline_entry()], "duplicate"),
        ([diagnostic()], [baseline_entry(reason="")], "reason"),
        ([diagnostic()], [baseline_entry(evidence="")], "evidence"),
        ([], [baseline_entry(code="E501", message="Line too long")], "not eligible"),
        ([diagnostic(path="README.md")], [], "outside"),
    ],
)
def test_schema_failures_exit_two(tmp_path, report, baseline, expected):
    result = run_gate(tmp_path, report, baseline)
    assert result.returncode == 2
    assert expected in result.stderr


def test_malformed_and_missing_reports_exit_two(tmp_path):
    malformed = tmp_path / "report.json"
    malformed.write_text("{", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(malformed)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    missing = subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(tmp_path / "missing.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert "unavailable" in missing.stderr
