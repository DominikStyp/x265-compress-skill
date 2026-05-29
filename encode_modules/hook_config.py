"""Hook config: parse the CLI/queue values into argv lists and (de)serialize
to a per-source JSON sidecar.

The sidecar (`<stem>.hooks.json`) is why an argv — which can contain `"` — never
needs to be embedded into a generated .bat/.sh: compress.py writes the lists
here and the generated script carries only the sidecar's *path*, so the
existing path-stash quoting in the templates keeps working untouched.

Schema is intentionally a flat top-level dict keyed by hook name, so additional
hooks (`on_job_end`, future `on_file_complete`) slot in without breaking
v1.8.x-and-earlier readers: an old reader looks up `on_chunk_done` and ignores
unknown keys; a new reader iterates the dict.

Two deliberately opposite failure modes:
  * parse_hook_spec  fails LOUD (a typo'd hook surfaces at compress.py time,
                     before any encode starts).
  * load_hook(s)_sidecar degrades to None / drops the invalid entry (a missing
                     or corrupt sidecar at encode time must never abort a
                     multi-hour encode — hooks are auxiliary).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


# Back-compat: v1.8.x sidecars carry only this key. Newer ones may carry more.
SIDECAR_KEY = "on_chunk_done"

# Hook keys this version knows about. Unknown keys in a sidecar are tolerated
# (degrade: ignored) — that's how we'll add `on_file_complete` later without
# breaking a sidecar mid-encode if the user upgrades while resuming.
VALID_HOOK_KEYS: tuple[str, ...] = ("on_chunk_done", "on_job_end")


def parse_hook_spec(value: str, *, key: str = SIDECAR_KEY) -> list[str]:
    """Turn a CLI / queue value into an argv list.

    A value starting with `[` is parsed as a JSON array — the canonical form,
    able to carry an interpreter + flags like `["pwsh","-File","x.ps1"]` whose
    `-File` would otherwise be mis-read as an option. Any other bare value is a
    single program token. Raises ValueError on anything that isn't a non-empty
    list of non-empty strings, so a typo can't silently become a no-op or a
    one-token garbage command. `key` is used only in the error message — the
    parser itself is hook-agnostic."""
    text = value.strip()
    if not text:
        raise ValueError(f"{key} is empty")
    if text.startswith("{"):
        raise ValueError(
            f"{key} must be a JSON array (argv list) or a bare command, "
            "not a JSON object")
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{key} is not valid JSON: {e}") from e
    else:
        parsed = [text]  # bare single token
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{key} must be a non-empty argv list")
    if not all(isinstance(item, str) and item for item in parsed):
        raise ValueError(f"{key} items must be non-empty strings")
    if any("\x00" in item for item in parsed):
        # A NUL makes subprocess.run raise ValueError at fire time; reject it
        # loud here (config error) rather than arm a worker-killing crash.
        raise ValueError(f"{key} items must not contain NUL characters")
    return parsed


def sidecar_path(tmp_dir: Path, stem: str) -> Path:
    """Where the hook sidecar lives for a given output stem."""
    return tmp_dir / f"{stem}.hooks.json"


def write_hook_sidecar(tmp_dir: Path, stem: str, command: list[str]) -> Path:
    """Back-compat single-key writer: writes only `on_chunk_done`. Kept so
    existing call sites in v1.8.x-and-earlier callers (and tests) keep working.
    New call sites should use `write_hooks_sidecar` for multi-hook support."""
    path = write_hooks_sidecar(tmp_dir, stem, on_chunk_done=command)
    # write_hooks_sidecar returns None when ALL hooks are None — that can only
    # happen for the back-compat call when the caller passed None/[]. Mirror
    # the old return convention (Path always) by re-resolving from tmp_dir.
    return path if path is not None else sidecar_path(tmp_dir, stem)


def write_hooks_sidecar(tmp_dir: Path, stem: str, *,
                        on_chunk_done: Optional[list[str]] = None,
                        on_job_end: Optional[list[str]] = None
                        ) -> Optional[Path]:
    """Atomic multi-hook writer. Pass any subset of the known hooks as kwargs;
    the resulting sidecar carries exactly those keys, in canonical order.

    Returns the sidecar path, or None when no hooks are configured (saves a
    pointless empty file on the resume path). Atomic via temp-then-os.replace:
    a kill mid-write can never leave a half-written sidecar at the final
    path (atomic-writes invariant)."""
    payload: dict[str, list[str]] = {}
    if on_chunk_done:
        payload["on_chunk_done"] = list(on_chunk_done)
    if on_job_end:
        payload["on_job_end"] = list(on_job_end)
    if not payload:
        return None
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dst = sidecar_path(tmp_dir, stem)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, dst)
    return dst


def _valid_command(value) -> bool:
    """A sidecar value is usable iff it's a non-empty list of non-empty,
    NUL-free strings — same shape parse_hook_spec produces."""
    return (isinstance(value, list) and bool(value)
            and all(isinstance(c, str) and c and "\x00" not in c
                    for c in value))


def load_hooks_sidecar(path: Optional[Path]) -> Optional[dict[str, list[str]]]:
    """Multi-hook reader: returns a dict mapping known hook names to their
    argv. Unknown keys are silently dropped; invalid entries (non-list, empty,
    NUL-bearing) are dropped per-key without affecting other keys. Returns
    None on missing/unreadable/empty-after-filtering sidecars.

    Degrades — never raises — same contract as the legacy reader, because an
    auxiliary-hook problem must never abort a multi-hour encode."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for key in VALID_HOOK_KEYS:
        value = data.get(key)
        if _valid_command(value):
            out[key] = list(value)
    return out or None


def load_hook_sidecar(path: Optional[Path]) -> Optional[list[str]]:
    """Back-compat single-key reader: returns only the `on_chunk_done` argv.
    The encoder's existing call site in encode_resumable still uses this for
    the chunk hook; the job-end hook is loaded via `load_hooks_sidecar`."""
    hooks = load_hooks_sidecar(path)
    if hooks is None:
        return None
    return hooks.get(SIDECAR_KEY)
