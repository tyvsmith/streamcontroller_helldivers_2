"""Process inspection helpers used only by lifecycle tests."""

from pathlib import Path


def process_is_live(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z"
    except FileNotFoundError:
        return False
