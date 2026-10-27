"""The "coverage must not drop" ratchet from docs/engineering-standards.md."""

from pathlib import Path

import coverage
import pytest

from scripts.check_coverage_baseline import (
    BaselineError,
    is_not_below,
    raise_baseline,
    read_baseline,
    total_coverage,
)

MODULE_SOURCE = """\
def a() -> int:
    return 1


def b() -> int:
    return 2
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_read_baseline(tmp_path: Path) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", "97.25\n")
    assert read_baseline(baseline) == 97.25


@pytest.mark.parametrize("content", ["", "abc", "101", "-1", "nan", "97.25 extra"])
def test_read_baseline_rejects_malformed(tmp_path: Path, content: str) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", content)
    with pytest.raises(BaselineError):
        read_baseline(baseline)


def test_read_baseline_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BaselineError):
        read_baseline(tmp_path / ".coverage-baseline")


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    [
        (97.25, 97.25, True),
        (98.0, 97.25, True),
        (97.249, 97.25, True),  # compared at two decimals, like the report
        (97.24, 97.25, False),
        (0.0, 0.01, False),
    ],
)
def test_is_not_below(current: float, baseline: float, expected: bool) -> None:
    assert is_not_below(current, baseline) is expected


def test_raise_baseline_moves_up(tmp_path: Path) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", "90.00\n")
    assert raise_baseline(baseline, 93.456) is True
    assert baseline.read_text() == "93.46\n"


def test_raise_baseline_never_moves_down(tmp_path: Path) -> None:
    baseline = _write(tmp_path / ".coverage-baseline", "90.00\n")
    assert raise_baseline(baseline, 80.0) is False
    assert baseline.read_text() == "90.00\n"


def test_total_coverage_from_data_file(tmp_path: Path) -> None:
    module = _write(tmp_path / "pkg" / "mod.py", MODULE_SOURCE)
    data_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_file))
    data.add_lines({str(module): [1, 2, 5]})
    data.write()

    assert total_coverage(data_file, config_file=False) == 75.0
