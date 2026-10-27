"""Coverage ratchet: total coverage must not drop below the committed baseline.

docs/engineering-standards.md: "coverage must not drop on a PR". The baseline lives in
`.coverage-baseline` (percent, two decimals) and CI fails when the total falls below it.
`--update` raises it when coverage improves and never lowers it; lowering it is a hand edit
that the PR must justify, so a reviewer sees it in the diff.

Run after the test suite: `uv run python -m scripts.check_coverage_baseline [--update]`.
"""

import argparse
import io
import math
import sys
from pathlib import Path

import coverage

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".coverage-baseline"
DATA_FILE = ROOT / ".coverage"
CONFIG_FILE = ROOT / "pyproject.toml"


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
        raise BaselineError(f"{path.name} must hold a percentage, e.g. 97.25") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise BaselineError(f"{path.name} must be between 0 and 100")
    return value


def is_not_below(current: float, baseline: float) -> bool:
    """Compare at two decimals, the precision the baseline is stored with."""
    return round(current, 2) >= round(baseline, 2)


def raise_baseline(path: Path, current: float) -> bool:
    """Store `current` if it beats the baseline. Returns whether the file changed."""
    if round(current, 2) <= read_baseline(path):
        return False
    path.write_text(f"{current:.2f}\n")
    return True


def total_coverage(data_file: Path, config_file: Path | bool) -> float:
    """Total coverage in percent, as `coverage report` computes it."""
    config: str | bool = config_file if isinstance(config_file, bool) else str(config_file)
    cov = coverage.Coverage(data_file=str(data_file), config_file=config)
    cov.load()
    return cov.report(file=io.StringIO())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="raise the baseline if beaten")
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

    if args.update:
        changed = raise_baseline(BASELINE_FILE, current)
        print(
            f"Coverage ratchet: baseline {'raised to' if changed else 'kept at'} "
            f"{read_baseline(BASELINE_FILE):.2f}%"
        )
        return 0
    print(f"Coverage ratchet: {current:.2f}% (baseline {baseline:.2f}%)")
    return 0 if is_not_below(current, baseline) else 1


if __name__ == "__main__":
    sys.exit(main())
