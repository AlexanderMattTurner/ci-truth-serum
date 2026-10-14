"""The one definition of how a script under ``.github/scripts/`` finds the repository root."""

from pathlib import Path


def find_repo_root(start: Path) -> Path:
    """The first directory at or above ``start`` that holds ``.git`` or
    ``pyproject.toml``. Raises when none does, so a moved file cannot point at a
    directory that does not exist."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists() or (directory / "pyproject.toml").is_file():
            return directory
    raise FileNotFoundError(f"no .git or pyproject.toml at or above {start}")
