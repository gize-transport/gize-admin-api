"""The package is installed and typed."""

from importlib import resources

import gize_api


def test_package_ships_py_typed_marker() -> None:
    assert resources.files(gize_api).joinpath("py.typed").is_file()
