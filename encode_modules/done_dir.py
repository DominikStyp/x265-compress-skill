"""done_dir resolution + the move-after-success step.

Optional opt-in behaviour: after a successful encode (`status == "ok"`), move
both the source and the output into a user-specified archive directory. The
move is gated on success only — every other terminal status leaves the files
where they are, because they may need re-running.

Non-negotiable data-safety invariant: the user's source file must NEVER be
lost. The move sequence is therefore:

  1. PRE-CHECK both destination paths. If either exists, refuse — don't
     silently overwrite.
  2. shutil.move(output, dest_output)  -- moves output first.
  3. shutil.move(source, dest_source)  -- source after.

If step 3 fails, the worst case is "output in done_dir, source still in
place" — recoverable, no data loss. shutil.move falls back to copy+delete
across filesystems, so a cross-volume archive (D:→E: on Windows, internal→
external on macOS) works without special-casing.

A done_dir that resolves to the SAME directory as the source is a no-op
(common when `done_dir: "."` is set in a queue defaults block and a job
already lives where it should). We never refuse for this — moving a file
onto itself is just nothing to do.

A done_dir that resolves UNDER the workdir is REFUSED — the encoder's
cleanup() would otherwise wipe the moved result. Defense-in-depth: refusing
at move time means the user finds out at the first job, not after the third.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class DoneDirRefusedError(Exception):
    """The done_dir move was refused — see message for which guard fired.
    Caller should leave both source and output where they are."""


@dataclass(frozen=True)
class MoveResult:
    """Final paths after the move (or the same paths when the move was a
    no-op). `moved` is False iff source/output were left in place because
    done_dir resolved to their own directory."""
    source_final: Path
    output_final: Path
    moved: bool


def resolve_done_dir(value: Optional[str], *,
                     base_dir: Path) -> Optional[Path]:
    """Turn a `--done-dir` / queue.json value into an absolute Path with the
    parent directories created (or return None when no done_dir was set).

    Resolution rules:
      1. None / empty → None.
      2. `~`/`~user` expansion via Path.expanduser() (works on Windows too —
         it expands to %USERPROFILE%).
      3. Absolute paths used as-is.
      4. Relative paths resolved against `base_dir` (NOT the shell cwd, which
         may differ when the user runs the encoder from a wrapper script).
      5. mkdir(parents=True, exist_ok=True) — happens at resolve time so the
         user sees a clear error at startup, not after a multi-hour encode.
         OSError from mkdir is re-raised so the caller can fail loud."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    expanded = Path(text).expanduser()
    resolved = (expanded if expanded.is_absolute()
                else base_dir / expanded).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def move_to_done_dir(*,
                     source: Path,
                     output: Path,
                     done_dir: Path,
                     workdir: Path,
                     sidecar_dir: Optional[Path] = None) -> MoveResult:
    """Move source + output into done_dir. Idempotent guards refuse if
    either destination exists or done_dir sits inside workdir. Returns the
    final paths.

    `sidecar_dir` (optional) is the `.tmp/` directory holding the per-source
    sidecar JSONs; when provided, the per-job hooks sidecar is deleted (the
    file's job is done) and the per-source preflight cache is left in place
    (it's content-keyed and useful if the same source ever returns)."""
    if _samefile_parent(source, done_dir):
        return MoveResult(source_final=source, output_final=output,
                          moved=False)

    if _is_inside(done_dir, workdir):
        raise DoneDirRefusedError(
            f"refusing to move into a workdir subtree: done_dir={done_dir} "
            f"is inside workdir={workdir}. cleanup() would delete it.")

    dest_source = done_dir / source.name
    dest_output = done_dir / output.name
    if dest_source.exists() or dest_output.exists():
        raise DoneDirRefusedError(
            f"refusing to overwrite an existing destination in {done_dir}: "
            f"{dest_source.name if dest_source.exists() else dest_output.name} "
            f"already exists. Move it aside or rename it manually.")

    # Output first: a step-3 failure then leaves source intact + output in
    # done_dir — recoverable. shutil.move handles cross-volume via copy-then-
    # delete (returns the new path on success).
    shutil.move(str(output), str(dest_output))
    shutil.move(str(source), str(dest_source))

    if sidecar_dir is not None:
        # Sidecars are keyed on the SOURCE stem by `script_writer.py`
        # (and the workdir uses the same key). For an `.mkv` source the
        # output stem is `<name>.x265`, which doesn't match — using
        # `output.stem` here would silently miss the cleanup and leak the
        # hooks sidecar into archive. Always source.stem.
        _cleanup_sidecars(sidecar_dir, source.stem)

    return MoveResult(source_final=dest_source,
                      output_final=dest_output, moved=True)


def _samefile_parent(file_a: Path, dir_b: Path) -> bool:
    """True if file_a's parent IS dir_b (cross-platform, case-insensitive on
    Windows). `Path.samefile` is the canonical comparison; fall back to a
    resolved-string compare when the files don't exist yet so the check
    doesn't raise on a freshly-created done_dir."""
    try:
        return file_a.parent.samefile(dir_b)
    except OSError:
        return file_a.parent.resolve() == dir_b.resolve()


def _is_inside(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or a descendant. Uses resolve() so a
    symlink jumping outside the workdir doesn't trick the check."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _cleanup_sidecars(sidecar_dir: Path, stem: str) -> None:
    """Hook sidecar is a per-job artifact (deleted); preflight stays
    (content-keyed cache, useful on a future re-encode); quality sidecar
    stays where the encoder put it (downstream tooling will look there)."""
    hooks = sidecar_dir / f"{stem}.hooks.json"
    try:
        if hooks.exists():
            hooks.unlink()
    except OSError:
        # Best-effort cleanup — a permissions issue here mustn't break the
        # move. Leaving the sidecar in place is harmless.
        pass
