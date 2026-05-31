"""Trivial smoke test — verifies pytest discovery and execution works.

Added by Phase 0 #1 (Bootstrap pytest framework). Safe to delete once
real test modules exist; kept for now as the canonical 'is pytest wired
up?' indicator."""


def test_truth():
    assert True


def test_import_boox():
    """The flat boox module imports without error on the current baseline.

    This will start asserting real behavior once Phase 1 #26 (package
    layout refactor) lands. For now it just confirms hrw's flat module
    is importable from the test environment."""
    import boox  # noqa: F401
