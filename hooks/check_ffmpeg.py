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

import os
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


def _find_brew() -> str | None:
    """Locate the brew binary even when its prefix isn't yet on PATH.

    On a fresh Apple Silicon Mac, `brew install` puts the binary at
    /opt/homebrew/bin/brew but does NOT auto-add /opt/homebrew/bin to
    PATH — the user has to add `eval "$(/opt/homebrew/bin/brew shellenv)"`
    to their ~/.zprofile manually. Until they do, `shutil.which("brew")`
    returns None even though brew is fully installed. Probing the two
    canonical install prefixes catches this case."""
    found = shutil.which("brew")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _try_install_macos() -> bool:
    """brew install ffmpeg — no sudo needed, brew is user-scoped."""
    brew = _find_brew()
    if not brew:
        _emit("ffmpeg not found and Homebrew is not installed.")
        _emit("Install Homebrew from https://brew.sh, then re-launch Claude Code.")
        return False
    _emit("ffmpeg not found. Installing via Homebrew (one-time, ~30s)...")
    try:
        subprocess.run([brew, "install", "ffmpeg"], check=True,
                       stdout=subprocess.DEVNULL)
        _emit("ffmpeg installed successfully.")
        # If brew was at /opt/homebrew/bin but not on PATH, the just-installed
        # ffmpeg lives in the same prefix — and is also off-PATH for this
        # session. Tell the user to refresh.
        if not have_ffmpeg():
            _emit("NOTE: ffmpeg is installed but its directory isn't on PATH "
                  "yet. Add `eval \"$(/opt/homebrew/bin/brew shellenv)\"` to "
                  "your ~/.zprofile, then restart Claude Code.")
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
    """Linux installs need root — we never auto-elevate from a plugin hook.
    Print the right command for the detected package manager.

    `sudo` prefix is omitted when we're already root (minimal containers,
    server boxes) since `sudo` may not even be installed there."""
    # os.geteuid() exists on POSIX only — fine here since this function
    # only runs when sys.platform is neither "darwin" nor "win32".
    sudo = "" if os.geteuid() == 0 else "sudo "
    _emit("ffmpeg not found. Install with one of:")
    managers = [
        ("apt-get", f"{sudo}apt install ffmpeg"),           # Debian/Ubuntu
        ("dnf",     f"{sudo}dnf install ffmpeg   "
                    "(Fedora/RHEL needs RPM Fusion: see rpmfusion.org/Configuration)"),
        ("pacman",  f"{sudo}pacman -S ffmpeg"),             # Arch
        ("zypper",  f"{sudo}zypper install ffmpeg"),        # openSUSE
        ("apk",     f"{sudo}apk add ffmpeg"),               # Alpine
    ]
    found_any = False
    for binary, cmd in managers:
        if shutil.which(binary):
            _emit(f"  {cmd}")
            found_any = True
    if not found_any:
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
