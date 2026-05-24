"""on_chunk_done hook config: parse the CLI/queue value into an argv list, and
(de)serialize it to a JSON sidecar.

The sidecar is why a hook argv — which contains `"` — never needs to be embedded
into a generated .bat/.sh: compress.py writes the list here and the generated
script carries only the sidecar's *path* (no `"`), so the existing path-stash
quoting in the templates keeps working untouched.

Two deliberately opposite failure modes:
  * parse_hook_spec  fails LOUD (a typo'd hook surfaces at compress.py time,
                     before any encode starts).
  * load_hook_sidecar degrades to None (a missing/corrupt sidecar at encode
                     time must never abort a multi-hour encode — the hook is
                     auxiliary).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


SIDECAR_KEY = "on_chunk_done"


def parse_hook_spec(value: str) -> list[str]:
    """Turn a --on-chunk-done / queue `on_chunk_done` value into an argv list.

    A value starting with `[` is parsed as a JSON array — the canonical form,
    able to carry an interpreter + flags like `["pwsh","-File","x.ps1"]` whose
    `-File` would otherwise be mis-read as an option. Any other bare value is a
    single program token. Raises ValueError on anything that isn't a non-empty
    list of non-empty strings, so a typo can't silently become a no-op or a
    one-token garbage command."""
    text = value.strip()
    if not text:
        raise ValueError("on_chunk_done is empty")
    if text.startswith("{"):
        raise ValueError(
            "on_chunk_done must be a JSON array (argv list) or a bare command, "
            "not a JSON object")
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"on_chunk_done is not valid JSON: {e}") from e
    else:
        parsed = [text]  # bare single token
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("on_chunk_done must be a non-empty argv list")
    if not all(isinstance(item, str) and item for item in parsed):
        raise ValueError("on_chunk_done items must be non-empty strings")
    if any("\x00" in item for item in parsed):
        # A NUL makes subprocess.run raise ValueError at fire time; reject it
        # loud here (config error) rather than arm a worker-killing crash.
        raise ValueError("on_chunk_done items must not contain NUL characters")
    return parsed


def sidecar_path(tmp_dir: Path, stem: str) -> Path:
    """Where the hook sidecar lives for a given output stem."""
    return tmp_dir / f"{stem}.hooks.json"


def write_hook_sidecar(tmp_dir: Path, stem: str, command: list[str]) -> Path:
    """Atomically write the hook command to <tmp_dir>/<stem>.hooks.json and
    return its path. Temp-then-os.replace so a kill mid-write can never leave a
    half-written sidecar at the final path (atomic-writes invariant)."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dst = sidecar_path(tmp_dir, stem)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(json.dumps({SIDECAR_KEY: command}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, dst)
    return dst


def load_hook_sidecar(path: Path | None) -> list[str] | None:
    """Read the hook command back from the sidecar. Returns None for a missing,
    unreadable, or malformed sidecar — an auxiliary-hook problem must never
    abort an encode (keep encoding, just skip the hook). The caller decides
    whether a None when one was expected warrants a warning."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        command = data.get(SIDECAR_KEY)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if (isinstance(command, list) and command
            and all(isinstance(c, str) and c and "\x00" not in c
                    for c in command)):
        return command
    return None
