#!/usr/bin/env bash
# x265-compress-skill installer (macOS / Linux).
#
# Two ways to invoke:
#   1. Curl-piped (fresh machine, no clone yet):
#        curl -fsSL https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.sh | bash
#      Will clone the repo into ~/.claude/skills/ffmpeg-compress-video, then
#      verify deps and run a smoke test.
#
#   2. Inside an existing clone (re-run after git pull, etc.):
#        bash install.sh
#      Skips the clone step, just verifies deps + smoke test.
#
# Non-interactive mode (for CI / automation):
#   - Pass --yes:    curl ... | bash -s -- --yes
#   - Or env var:    INSTALL_YES=1 curl ... | bash
#   - Or CI=1 is honoured the same way.
#
# Override the install location:
#   - SKILL_DIR=/some/where curl ... | bash
#   - Defaults to $HOME/.claude/skills/ffmpeg-compress-video.

set -euo pipefail

REPO_URL="https://github.com/DominikStyp/x265-compress-skill.git"
DEFAULT_SKILL_DIR="$HOME/.claude/skills/ffmpeg-compress-video"

# --- Detect: are we running from inside a clone, or piped fresh? ------------
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$( cd "$( dirname "$SCRIPT_PATH" )" 2>/dev/null && pwd )" || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/SKILL.md" ]; then
    SKILL_DIR="$SCRIPT_DIR"
    NEEDS_CLONE=0
else
    SKILL_DIR="${SKILL_DIR:-$DEFAULT_SKILL_DIR}"
    NEEDS_CLONE=1
fi

# --- Non-interactive flag ---------------------------------------------------
AUTO_YES=0
if [ "${1:-}" = "--yes" ] || [ "${INSTALL_YES:-}" = "1" ] || [ "${CI:-}" = "1" ]; then
    AUTO_YES=1
fi

C_GREEN='\033[32;1m'
C_RED='\033[31;1m'
C_YELLOW='\033[33;1m'
C_RESET='\033[0m'

info()    { printf "${C_GREEN}==>${C_RESET} %s\n" "$*"; }
warn()    { printf "${C_YELLOW}!!${C_RESET}  %s\n" "$*"; }
err()     { printf "${C_RED}ERR${C_RESET} %s\n" "$*" >&2; }

# Read with /dev/tty fallback so prompts work even when stdin is the install
# script itself (the curl|bash case).
confirm() {
    [ "$AUTO_YES" = "1" ] && return 0
    if [ -t 0 ]; then
        printf "    %s [Y/n] " "$1"
        read -r ans
    elif [ -c /dev/tty ]; then
        printf "    %s [Y/n] " "$1" > /dev/tty
        read -r ans < /dev/tty
    else
        # No interactive input available and no AUTO_YES — bail safely.
        warn "non-interactive shell and no --yes flag; assuming 'no'"
        return 1
    fi
    [[ ! "$ans" =~ ^[Nn] ]]
}

# ---------------------------------------------------------------------------
# Step 0: git (only needed for bootstrap)
# ---------------------------------------------------------------------------
if [ "$NEEDS_CLONE" = "1" ]; then
    if ! command -v git >/dev/null 2>&1; then
        err "git not found on PATH — required to clone the repo."
        case "$(uname -s)" in
            Darwin) echo "    Install: xcode-select --install   (or brew install git)" ;;
            Linux)  echo "    Install: sudo apt install git   /   sudo dnf install git" ;;
        esac
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 1: Clone (only when bootstrapping)
# ---------------------------------------------------------------------------
if [ "$NEEDS_CLONE" = "1" ]; then
    if [ -d "$SKILL_DIR/.git" ]; then
        info "Repo already at $SKILL_DIR — pulling latest"
        (cd "$SKILL_DIR" && git pull --ff-only) || warn "git pull failed; continuing with existing tree"
    elif [ -e "$SKILL_DIR" ]; then
        err "$SKILL_DIR exists and is not a git clone — refusing to overwrite."
        echo "    Move/delete it first, or pick a different SKILL_DIR."
        exit 1
    else
        info "Cloning $REPO_URL to $SKILL_DIR"
        mkdir -p "$(dirname "$SKILL_DIR")"
        git clone --depth 1 "$REPO_URL" "$SKILL_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Python 3.9+
# ---------------------------------------------------------------------------
info "Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found on PATH."
    case "$(uname -s)" in
        Darwin)  echo "    Install: brew install python  (or from python.org)" ;;
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
# Step 3: ffmpeg + ffprobe
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
# Step 4: Smoke test the skill's import graph
# ---------------------------------------------------------------------------
info "Verifying skill imports..."
PYTHONIOENCODING=utf-8 python3 "$SKILL_DIR/_smoke_test.py" "$SKILL_DIR"

# ---------------------------------------------------------------------------
# Step 5: Where it lives + next steps
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
