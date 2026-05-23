#!/usr/bin/env bash
# x265-compress-skill installer (macOS / Linux).
#
# Idempotent — safe to re-run. Checks Python + ffmpeg + ffprobe, offers
# to install ffmpeg via the system package manager if missing, runs a
# smoke import test, prints next steps.
#
# Auto-yes mode: pass --yes (or set $CI=1) to accept every prompt.

set -euo pipefail

SKILL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
AUTO_YES=0
if [ "${1:-}" = "--yes" ] || [ "${CI:-}" = "1" ]; then
    AUTO_YES=1
fi

C_GREEN='\033[32;1m'
C_RED='\033[31;1m'
C_YELLOW='\033[33;1m'
C_RESET='\033[0m'

info()    { printf "${C_GREEN}==>${C_RESET} %s\n" "$*"; }
warn()    { printf "${C_YELLOW}!!${C_RESET}  %s\n" "$*"; }
err()     { printf "${C_RED}ERR${C_RESET} %s\n" "$*" >&2; }

confirm() {
    # confirm "Question text"  ->  returns 0 on yes, 1 on no
    [ "$AUTO_YES" = "1" ] && return 0
    printf "    %s [Y/n] " "$1"
    read -r ans
    [[ ! "$ans" =~ ^[Nn] ]]
}

# ---------------------------------------------------------------------------
# Step 1: Python 3.9+
# ---------------------------------------------------------------------------
info "Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found on PATH."
    case "$(uname -s)" in
        Darwin)  echo "    Install: brew install python  (or use the official installer at python.org)" ;;
        Linux)   echo "    Install: sudo apt install python3   /   sudo dnf install python3" ;;
        *)       echo "    Install Python 3.9+ from https://www.python.org/downloads/" ;;
    esac
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    err "Python 3.9+ required, found $PY_VER."
    exit 1
fi
info "Python $PY_VER OK"

# ---------------------------------------------------------------------------
# Step 2: ffmpeg + ffprobe
# ---------------------------------------------------------------------------
install_ffmpeg() {
    case "$(uname -s)" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                info "Installing ffmpeg via Homebrew..."
                brew install ffmpeg
            else
                err "Homebrew not found. Install from https://brew.sh first, or install ffmpeg manually from https://ffmpeg.org/download.html"
                exit 1
            fi ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                info "Installing ffmpeg via apt..."
                sudo apt-get update && sudo apt-get install -y ffmpeg
            elif command -v dnf >/dev/null 2>&1; then
                info "Installing ffmpeg via dnf..."
                sudo dnf install -y ffmpeg
            elif command -v pacman >/dev/null 2>&1; then
                info "Installing ffmpeg via pacman..."
                sudo pacman -S --noconfirm ffmpeg
            else
                err "No supported package manager (apt/dnf/pacman). Install ffmpeg manually."
                exit 1
            fi ;;
        *)
            err "Unknown OS $(uname -s). Install ffmpeg manually."
            exit 1 ;;
    esac
}

info "Checking ffmpeg + ffprobe..."
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    warn "ffmpeg/ffprobe not on PATH."
    if confirm "Install ffmpeg now?"; then
        install_ffmpeg
    else
        err "ffmpeg required. Install it and re-run this installer."
        exit 1
    fi
fi
FFMPEG_VER=$(ffmpeg -version | head -n1)
info "$FFMPEG_VER"

# ---------------------------------------------------------------------------
# Step 3: Smoke test the skill's import graph
# ---------------------------------------------------------------------------
info "Verifying skill imports..."
PYTHONIOENCODING=utf-8 python3 -c "
import sys
sys.path.insert(0, '$SKILL_DIR')
import platform_compat as pc
from compress_modules import probe, plan, script_writer
from encode_modules import encoder, display
from queue_modules import job_runner, job_schema, queue_io
print('  OS detected:', pc.os_name())
print('  Script extension:', script_writer.SCRIPT_EXTENSION)
"

# ---------------------------------------------------------------------------
# Step 4: Where it lives + next steps
# ---------------------------------------------------------------------------
info "Installation complete."
echo
echo "    Skill location:  $SKILL_DIR"
echo
echo "    The skill activates automatically in Claude Code on prompts like:"
echo "      'compress this video'"
echo "      'shrink this mp4'"
echo "      'compress all videos in <folder>'"
echo
echo "    Standalone (no Claude Code):"
echo "      python3 $SKILL_DIR/compress.py /path/to/video.mp4 --resumable"
echo
echo "    See $SKILL_DIR/SKILL.md for the agent playbook,"
echo "    and $SKILL_DIR/docs/AGENT_QUEUE_RECIPES.md for queue.json templates."
