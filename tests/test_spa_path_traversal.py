from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.main import _resolve_spa_file  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def fe_dist(tmp_path: Path) -> Path:
    """A built-frontend dist/ directory with a secret file alongside it."""
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "favicon.ico").write_text("legit")
    # a sensitive file OUTSIDE the dist directory, several levels up
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return dist.resolve()


@pytest.mark.parametrize(
    "attack",
    [
        "../../secret.txt",
        "../../../secret.txt",
        "../../../../../../../../etc/passwd",
        # pathlib discards the left operand when the right is absolute
        "/etc/passwd",
        # nested traversal that briefly dips inside before escaping
        "assets/../../../secret.txt",
    ],
)
def test_traversal_falls_back_to_index(fe_dist: Path, attack: str) -> None:
    """Any path that escapes the dist directory must resolve to index.html,
    never a file off the host (CWE-22)."""
    resolved = _resolve_spa_file(fe_dist, attack)
    assert resolved == fe_dist / "index.html"


def test_escaping_path_does_not_leak_real_file(fe_dist: Path) -> None:
    """The out-of-tree secret must never be the resolved target."""
    secret = (fe_dist.parent.parent / "secret.txt").resolve()
    assert secret.is_file()  # sanity: the bait file exists and is readable
    resolved = _resolve_spa_file(fe_dist, "../../secret.txt")
    assert resolved != secret


@pytest.mark.parametrize("name", ["index.html", "favicon.ico"])
def test_legitimate_files_still_serve(fe_dist: Path, name: str) -> None:
    resolved = _resolve_spa_file(fe_dist, name)
    assert resolved == fe_dist / name
    assert resolved.is_file()


def test_unknown_in_tree_path_falls_back_to_index(fe_dist: Path) -> None:
    """A normal SPA client route (no such file) serves the entrypoint."""
    resolved = _resolve_spa_file(fe_dist, "some/client/route")
    assert resolved == fe_dist / "index.html"
