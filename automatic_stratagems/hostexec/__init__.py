"""Scripts launched on the host, outside the Flatpak sandbox, by /usr/bin/python3."""

from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent


def script(name: str) -> Path:
    """Return the path to a hostexec script by filename."""
    return DIRECTORY / name
