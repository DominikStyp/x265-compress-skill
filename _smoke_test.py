"""Post-install smoke test. Imports every public module in the skill so
the installer can fail-fast if anything's miswired. Called by install.sh
and install.ps1 with the skill directory as argv[1].

Prints two lines on success:
    OS detected: <name>
    Script extension: <.bat or .sh>

Exits non-zero on any import failure (which then propagates out of the
installer)."""
from __future__ import annotations

import sys
from pathlib import Path

if len(sys.argv) >= 2:
    sys.path.insert(0, sys.argv[1])
else:
    # Allow running from inside the skill dir for ad-hoc verification.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import platform_compat as pc
from compress_modules import (  # noqa: F401
    plan, probe, script_writer, x265_params,
)
from encode_modules import (  # noqa: F401
    chunking, chunk_recovery, chunk_worker, display, encoder,
    encode_parallel, encode_serial, keyboard_input, messages,
    pre_flight, preflight_decision, source_patcher, verify, verify_loop,
)
from queue_modules import job_runner, job_schema, queue_io  # noqa: F401

print(f"  OS detected: {pc.os_name()}")
print(f"  Script extension: {script_writer.SCRIPT_EXTENSION}")
