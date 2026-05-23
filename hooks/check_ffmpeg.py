"""SessionStart hook: verify ffmpeg + ffprobe are on PATH; auto-install if not.

Runs once per Claude Code session start (matcher: "startup"). The check is
fast on the common case — both binaries already installed — and only
triggers an install attempt when something is missing.

OS dispatch:
  * macOS:   tries `brew install ffmpeg` (user scope, no sudo)
  * Windows: tries `winget install --scope user Gyan.FFmpeg`
  * Linux:   prints the apt/dnf/pacman command (no auto-sudo)

Output convention:
  * stdout silent  -> session loads cleanly, user sees nothing
  * stderr message + exit 2 -> shown to user; convey what happened
  * exit 0 -> ffmpeg present (or successfully installed)
  * exit 2 -> ffmpeg missing AND auto-install declined / unavailable

The hook does NOT block the session. The user may type before the
install completes; SKILL.md's agent playbook tells the agent to verify
ffmpeg before invoking compress.py as a safety net.
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def have_ffmpeg() -> bool:
    """True iff both ffmpeg and ffprobe are resolvable on PATH."""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _emit(msg: str) -> None:
    """Print to stderr — Claude Code surfaces stderr to the user when the
    hook exits non-zero. stdout is suppressed by convention."""
    print(f"[x265-compress-skill] {msg}", file=sys.stderr)


def _try_install_macos() -> bool:
    """brew install ffmpeg — no sudo needed, brew is user-scoped."""
    if not shutil.which("brew"):
        _emit("ffmpeg not found and Homebrew is not installed.")
        _emit("Install Homebrew from https://brew.sh, then re-launch Claude Code.")
        return False
    _emit("ffmpeg not found. Installing via Homebrew (one-time, ~30s)...")
    try:
        subprocess.run(["brew", "install", "ffmpeg"], check=True,
                       stdout=subprocess.DEVNULL)
        _emit("ffmpeg installed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        _emit(f"brew install failed (exit {e.returncode}). "
              "Run `brew install ffmpeg` manually.")
        return False


def _try_install_windows() -> bool:
    """winget install --scope user — no UAC elevation needed."""
    if not shutil.which("winget"):
        _emit("ffmpeg not found and winget is not available.")
        _emit("Install ffmpeg from https://ffmpeg.org/download.html, "
              "then re-launch Claude Code.")
        return False
    _emit("ffmpeg not found. Installing via winget (one-time, ~30s)...")
    try:
        subprocess.run(
            ["winget", "install", "--id", "Gyan.FFmpeg",
             "--silent", "--scope", "user",
             "--accept-package-agreements", "--accept-source-agreements"],
            check=True, stdout=subprocess.DEVNULL,
        )
        _emit("ffmpeg installed successfully.")
        _emit("NOTE: winget --scope user puts ffmpeg on PATH after a NEW "
              "shell session — restart Claude Code once more for the "
              "skill to detect it.")
        return True
    except subprocess.CalledProcessError as e:
        _emit(f"winget install failed (exit {e.returncode}). "
              "Run `winget install --id Gyan.FFmpeg` manually.")
        return False


def _print_linux_instructions() -> None:
    """Linux installs need sudo — we never auto-elevate from a plugin hook.
    Print the right command for the detected package manager."""
    _emit("ffmpeg not found. Install with one of:")
    if shutil.which("apt-get"):
        _emit("  sudo apt install ffmpeg")
    if shutil.which("dnf"):
        _emit("  sudo dnf install ffmpeg")
    if shutil.which("pacman"):
        _emit("  sudo pacman -S ffmpeg")
    if not any(shutil.which(p) for p in ("apt-get", "dnf", "pacman")):
        _emit("  (no supported package manager detected; install manually "
              "from https://ffmpeg.org/download.html)")


def main() -> int:
    if have_ffmpeg():
        return 0

    if sys.platform == "darwin":
        installed = _try_install_macos()
    elif sys.platform == "win32":
        installed = _try_install_windows()
    else:
        _print_linux_instructions()
        installed = False

    if installed and have_ffmpeg():
        return 0
    # Non-zero so Claude Code surfaces the stderr message to the user.
    return 2


if __name__ == "__main__":
    sys.exit(main())
