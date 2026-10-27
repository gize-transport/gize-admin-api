"""Coverage ratchet: total coverage must equal the committed baseline.

docs/engineering-standards.md: "coverage must not drop on a PR". `.coverage-baseline` holds
main's total coverage (percent, four decimals, rounded down). CI fails when coverage drops
below it, and also when coverage rises without the baseline being raised, so the baseline
always tracks main and a later PR cannot quietly give the gain back.

`--update` raises the baseline to the current total and refuses to lower it. Lowering it is
a hand edit that the PR must justify, so a reviewer sees it in the diff.

Run after the test suite: `uv run python -m scripts.check_coverage_baseline [--update]`.
"""

import argparse
import io
import math
import sys
from pathlib import Path
from typing import Literal

import coverage

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".coverage-baseline"
DATA_FILE = ROOT / ".coverage"
CONFIG_FILE: Path | bool = ROOT / "pyproject.toml"

DECIMALS = 4

Comparison = Literal["dropped", "equal", "improved"]


class BaselineError(Exception):
    """The baseline file is missing or does not hold a percentage."""


def read_baseline(path: Path) -> float:
    """The committed baseline percentage."""
    try:
        text = path.read_text().strip()
    except FileNotFoundError as exc:
        raise BaselineError(f"{path.name} not found") from exc
    try:
        value = float(text)
    except ValueError as exc:
        raise BaselineError(f"{path.name} must hold a percentage, e.g. 97.2534") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise BaselineError(f"{path.name} must be between 0 and 100")
    return value


def floor_percent(value: float) -> float:
    """Round down to the stored precision, so a stored baseline never exceeds the truth."""
    scale: int = 10**DECIMALS
    return math.floor(value * scale + 1e-9) / scale


def compare(current: float, baseline: float) -> Comparison:
    """How the current total relates to the baseline, at the stored precision."""
    stored = floor_percent(current)
    if stored < baseline:
        return "dropped"
    if stored > baseline:
        return "improved"
    return "equal"


def total_coverage(data_file: Path, config_file: Path | bool) -> float:
    """Total coverage in percent, computed like `coverage report` (lines and branches)."""
    config: str | bool = config_file if isinstance(config_file, bool) else str(config_file)
    cov = coverage.Coverage(data_file=str(data_file), config_file=config)
    cov.load()
    return cov.report(file=io.StringIO())


def main() -> int:
    parser = argparse.ArgumentParser(description="Coverage must equal .coverage-baseline.")
    parser.add_argument("--update", action="store_true", help="raise the baseline to current")
    args = parser.parse_args()

    if not DATA_FILE.exists():
        print("Coverage ratchet: no .coverage file; run `uv run pytest` first.")
        return 1
    try:
        baseline = read_baseline(BASELINE_FILE)
    except BaselineError as exc:
        print(f"Coverage ratchet: {exc}")
        return 1
    current = total_coverage(DATA_FILE, CONFIG_FILE)
    result = compare(current, baseline)
    summary = f"{floor_percent(current):.{DECIMALS}f}% vs baseline {baseline:.{DECIMALS}f}%"

    if result == "dropped":
        print(f"Coverage ratchet: coverage dropped, {summary}. Add tests for the new code.")
        return 1
    if result == "equal":
        print(f"Coverage ratchet: ok, {summary}.")
        return 0
    if args.update:
        BASELINE_FILE.write_text(f"{floor_percent(current):.{DECIMALS}f}\n")
        print(f"Coverage ratchet: baseline raised, {summary}. Commit .coverage-baseline.")
        return 0
    print(
        f"Coverage ratchet: coverage improved, {summary}. Run with --update and commit "
        ".coverage-baseline."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
