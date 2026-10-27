"""Services coverage gate: service modules must reach 90% line coverage.

docs/engineering-standards.md requires 90% for the service layer on top of the 85% overall
gate in pyproject.toml. Service modules are `<domain>/service.py` (docs/architecture.md)
and anything in a `services/` package. Modules no test imported count as 0%.

Run after the test suite: `uv run pytest && uv run python -m scripts.check_service_coverage`.
"""

import sys
from pathlib import Path

import coverage

THRESHOLD = 90.0
ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "src" / "gize_api"
DATA_FILE = ROOT / ".coverage"


def find_service_modules(package_dir: Path) -> list[Path]:
    """Every service-layer module under the package, sorted for stable output."""
    found = set(package_dir.glob("**/service.py")) | set(package_dir.glob("**/services/*.py"))
    return sorted(found)


def service_coverage(data_file: Path, modules: list[Path]) -> float:
    """Combined line coverage of `modules`, in percent, from a coverage data file."""
    cov = coverage.Coverage(data_file=str(data_file), config_file=False)
    cov.load()
    statements = 0
    executed = 0
    for module in modules:
        _, module_statements, _, missing, _ = cov.analysis2(str(module))
        statements += len(module_statements)
        executed += len(module_statements) - len(missing)
        print(f"{module.relative_to(module.parents[2])}: {len(missing)} lines missing")
    return 100.0 if statements == 0 else 100.0 * executed / statements


def main() -> int:
    modules = find_service_modules(PACKAGE_DIR)
    if not modules:
        print("Services coverage gate: no service modules yet, nothing to check.")
        return 0
    if not DATA_FILE.exists():
        print("Services coverage gate: no .coverage file; run `uv run pytest` first.")
        return 1
    percent = service_coverage(DATA_FILE, modules)
    print(f"Services coverage: {percent:.2f}% (required {THRESHOLD:.0f}%)")
    return 0 if percent >= THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
