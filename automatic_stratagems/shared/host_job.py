"""Bounded host-job identity shared by the scanner, its parent and hostexec."""

import json
from dataclasses import dataclass
import os
from pathlib import Path
import stat


OWNER = "net_jslay_helldivers_2"
VERSION = 1
REGISTRY_FILE = "registry.json"
STATE_LIMIT = 4096


class HostCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostJob:
    path: Path
    nonce: str
    hard_deadline: int


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                metadata.st_size > STATE_LIMIT):
            raise HostCommandError(f"Unsafe host command state: {path}")
        chunks = []
        remaining = STATE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > STATE_LIMIT:
            raise HostCommandError(f"Unsafe host command state: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(payload.decode())
    if not isinstance(value, dict):
        raise HostCommandError(f"Invalid host command state: {path}")
    return value


def _registry_value(job):
    return {"version": VERSION, "owner": OWNER, "nonce": job.nonce,
            "hard_deadline": job.hard_deadline}


def validate_job(job):
    metadata = job.path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or
            stat.S_IMODE(metadata.st_mode) != 0o700):
        raise HostCommandError(f"Unsafe host job directory: {job.path}")
    if _read_json(job.path / REGISTRY_FILE) != _registry_value(job):
        raise HostCommandError("Host job registry identity changed")


_validate_job = validate_job
