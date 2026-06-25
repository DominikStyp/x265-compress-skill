"""Shared test scaffolding (v1.20.1, finding #6).

The `RecordingRunner` subprocess stand-in was copy-pasted byte-for-byte across
the hook test modules. Consolidated here so a change to its contract (e.g. a
new CompletedProcess field) is a one-file edit. Imported as
``from tests._helpers import RecordingRunner`` (tests/ is a package and the
test modules put the repo root on sys.path).
"""
from __future__ import annotations

import types


class RecordingRunner:
    """Stand-in for ``subprocess.run``: records every ``(args, kwargs)`` call,
    returns a fake completed process exposing ``returncode``/``stderr``, or
    raises a preset exception — so hook tests never spawn a real process."""

    def __init__(self, returncode: int = 0, stderr: str = "", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(returncode=self.returncode,
                                     stderr=self.stderr)
