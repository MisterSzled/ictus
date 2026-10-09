"""The installed version has to be one somebody can read about.

The stdlib's constructors are the API. This release removed one, moved four out
of the flat namespace and renamed eleven modules — all of it invisible to anyone
downstream, because nothing but a changelog can carry that. A version with no
entry is the failure: it means a release went out and the only record of what
broke is the diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import ictus

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
TEXT = CHANGELOG.read_text(encoding="utf-8")

#: `## [1.2.3] — 2026-10-08`, the Keep a Changelog release heading.
RELEASES = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", TEXT, re.M)


def test_the_changelog_exists_at_the_project_root() -> None:
    assert CHANGELOG.is_file()


def test_it_records_at_least_one_release() -> None:
    """A heading format that stopped matching would make the test below vacuous."""
    assert RELEASES


def test_the_installed_version_has_an_entry() -> None:
    """Shipping a version nobody can read about is the thing this prevents."""
    assert ictus.__version__ in RELEASES, (
        f"ictus {ictus.__version__} is installed and CHANGELOG.md documents "
        f"{RELEASES}. Add an entry before releasing it."
    )


def test_the_version_is_reported_from_the_installed_metadata() -> None:
    """Not a second copy in the source, which is the usual way these diverge."""
    from importlib.metadata import version

    assert ictus.__version__ == version("ictus")


def test_releases_are_newest_first() -> None:
    order = [tuple(int(part) for part in v.split(".")) for v in RELEASES]
    assert order == sorted(order, reverse=True)


def test_the_readme_points_at_it() -> None:
    assert "CHANGELOG.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_move_it_claims_actually_landed() -> None:
    """The "Now" column of the moves table has to name modules that exist.

    A changelog telling somebody to import from a path that was never created is
    worse than silence: they will believe it before they believe the traceback.
    """
    import importlib

    claimed = sorted(set(re.findall(r"^\| `ictus\.[^`]+` \| `(ictus\.[a-z_.]+)`", TEXT, re.M)))
    assert claimed, "the moves table stopped parsing; this test is asserting nothing"
    missing = []
    for module in claimed:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    assert not missing, f"CHANGELOG.md sends people to modules that do not exist: {missing}"
