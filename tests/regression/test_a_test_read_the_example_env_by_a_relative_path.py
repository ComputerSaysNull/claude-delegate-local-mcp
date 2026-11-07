"""A test read `.env.example` relative to the working directory and failed once on CI.

`test_the_example_file_no_longer_ships_a_root_that_cannot_load` opened `Path(".env.example")`,
so it passed only while the worker sat in the repository root, and a CI run met it
elsewhere. It now reads the file by the repository's own path.
"""

from test_init import test_the_example_file_no_longer_ships_a_root_that_cannot_load as check


def test_it_passes_from_another_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    check()
