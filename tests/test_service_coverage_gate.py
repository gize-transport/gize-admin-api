"""The services coverage gate (90%) from docs/engineering-standards.md."""

from pathlib import Path

import coverage

from scripts.check_service_coverage import THRESHOLD, find_service_modules, service_coverage

SERVICE_SOURCE = """\
def assign(seats: int) -> int:
    if seats > 0:
        return seats - 1
    return 0


def release(seats: int) -> int:
    return seats + 1
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _record_lines(data_file: Path, measured: dict[Path, list[int]]) -> None:
    data = coverage.CoverageData(basename=str(data_file))
    data.add_lines({str(path): lines for path, lines in measured.items()})
    data.write()


def test_threshold_is_90_percent() -> None:
    assert THRESHOLD == 90.0


def test_finds_domain_service_modules_and_services_packages(tmp_path: Path) -> None:
    package = tmp_path / "gize_api"
    routes = _write(package / "routes" / "service.py", "")
    riders = _write(package / "riders" / "services" / "assign.py", "")
    _write(package / "routes" / "router.py", "")
    _write(package / "core" / "config.py", "")

    assert find_service_modules(package) == sorted([routes, riders])


def test_no_service_modules_means_gate_not_applicable(tmp_path: Path) -> None:
    assert find_service_modules(tmp_path / "gize_api") == []


def test_fully_covered_service_passes(tmp_path: Path) -> None:
    module = _write(tmp_path / "gize_api" / "routes" / "service.py", SERVICE_SOURCE)
    data_file = tmp_path / ".coverage"
    _record_lines(data_file, {module: [1, 2, 3, 4, 7, 8]})

    assert service_coverage(data_file, [module]) == 100.0


def test_partially_covered_service_is_below_threshold(tmp_path: Path) -> None:
    module = _write(tmp_path / "gize_api" / "routes" / "service.py", SERVICE_SOURCE)
    data_file = tmp_path / ".coverage"
    _record_lines(data_file, {module: [1, 2, 3, 7]})

    assert service_coverage(data_file, [module]) < THRESHOLD


def test_service_never_imported_counts_as_zero(tmp_path: Path) -> None:
    covered = _write(tmp_path / "gize_api" / "routes" / "service.py", SERVICE_SOURCE)
    unimported = _write(tmp_path / "gize_api" / "riders" / "service.py", SERVICE_SOURCE)
    data_file = tmp_path / ".coverage"
    _record_lines(data_file, {covered: [1, 2, 3, 4, 7, 8]})

    assert service_coverage(data_file, [covered, unimported]) == 50.0
