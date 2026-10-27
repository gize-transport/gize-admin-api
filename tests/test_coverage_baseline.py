"""The "coverage must not drop" ratchet from docs/engineering-standards.md."""

import sys
from pathlib import Path

import coverage
import pytest

from scripts import check_coverage_baseline as ratchet
from scripts.check_coverage_baseline import (
    BaselineError,
    compare,
    floor_percent,
    read_baseline,
    total_coverage,
)

MODULE_SOURCE = """\
def a(flag: bool) -> int:
    if flag:
        return 1
    return 2


def b() -> int:
    return 3
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _line_data(data_file: Path, module: Path, lines: list[int]) -> None:
    data = coverage.CoverageData(basename=str(data_file))
    data.add_lines({str(module): lines})
    data.write()


# --- read_baseline --------------------------------------------------------------------


def test_read_baseline(tmp_path: Path) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", "97.2534\n")
    assert read_baseline(baseline) == 97.2534


@pytest.mark.parametrize("content", ["", "abc", "100.0001", "-1", "nan", "inf", "97.25 extra"])
def test_read_baseline_rejects_malformed(tmp_path: Path, content: str) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", content)
    with pytest.raises(BaselineError):
        read_baseline(baseline)


def test_read_baseline_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BaselineError):
        read_baseline(tmp_path / ".coverage-baseline")


# --- compare --------------------------------------------------------------------------


def test_floor_percent_never_rounds_up() -> None:
    assert floor_percent(99.99999) == 99.9999
    assert floor_percent(100.0) == 100.0


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    [
        (97.25, 97.25, "equal"),
        (97.25009, 97.25, "equal"),  # below storage precision
        (97.2499, 97.25, "dropped"),
        (99.996, 100.0, "dropped"),  # rounding must not hide a real drop
        (97.2501, 97.25, "improved"),
        (100.0, 0.0, "improved"),
    ],
)
def test_compare(current: float, baseline: float, expected: str) -> None:
    assert compare(current, baseline) == expected


# --- total_coverage -------------------------------------------------------------------


def test_total_coverage_line_data(tmp_path: Path) -> None:
    module = _write(tmp_path / "pkg" / "mod.py", MODULE_SOURCE)
    data_file = tmp_path / ".coverage"
    _line_data(data_file, module, [1, 2, 3, 7, 8])

    assert total_coverage(data_file, config_file=False) == pytest.approx(5 / 6 * 100)


def test_total_coverage_branch_data_with_config_file(tmp_path: Path) -> None:
    module = _write(tmp_path / "pkg" / "mod.py", MODULE_SOURCE)
    config = _write(tmp_path / ".coveragerc", "[run]\nbranch = True\n")
    data_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_file))
    # Import the module, call a(True) and b(): the `if` never falls through to `return 2`.
    module_arcs = [(-1, 1), (1, 7), (7, -1)]
    a_true_arcs = [(-1, 2), (2, 3), (3, -1)]
    b_arcs = [(-7, 8), (8, -7)]
    data.add_arcs({str(module): module_arcs + a_true_arcs + b_arcs})
    data.write()

    # 6 statements (5 run) + 2 branch exits (1 taken) -> 6 of 8.
    assert total_coverage(data_file, config_file=config) == pytest.approx(75.0)


# --- main (what CI runs) --------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake project: one module 5/6 covered (83.3333%), no config file."""
    module = _write(tmp_path / "pkg" / "mod.py", MODULE_SOURCE)
    _line_data(tmp_path / ".coverage", module, [1, 2, 3, 7, 8])
    monkeypatch.setattr(ratchet, "DATA_FILE", tmp_path / ".coverage")
    monkeypatch.setattr(ratchet, "BASELINE_FILE", tmp_path / ".coverage-baseline")
    monkeypatch.setattr(ratchet, "CONFIG_FILE", False)
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["check_coverage_baseline", *args])
    return ratchet.main()


def test_main_passes_when_equal(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project / ".coverage-baseline", "83.3333\n")
    assert _run(monkeypatch) == 0


def test_main_fails_when_dropped(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project / ".coverage-baseline", "83.3334\n")
    assert _run(monkeypatch) == 1


def test_main_fails_when_improved_without_update(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project / ".coverage-baseline", "80.0000\n")
    assert _run(monkeypatch) == 1


def test_main_update_raises_baseline(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _write(project / ".coverage-baseline", "80.0000\n")
    assert _run(monkeypatch, "--update") == 0
    assert baseline.read_text() == "83.3333\n"
    assert _run(monkeypatch) == 0


def test_main_update_refuses_to_lower(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _write(project / ".coverage-baseline", "90.0000\n")
    assert _run(monkeypatch, "--update") == 1
    assert baseline.read_text() == "90.0000\n"


def test_main_fails_without_coverage_data(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project / ".coverage-baseline", "83.3333\n")
    (project / ".coverage").unlink()
    assert _run(monkeypatch) == 1


def test_main_fails_on_malformed_baseline(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project / ".coverage-baseline", "lots\n")
    assert _run(monkeypatch) == 1
