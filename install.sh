#!/usr/bin/env bash
# x265-compress-skill installer (macOS / Linux).
#
# Two ways to invoke:
#   1. Curl-piped (fresh machine, no clone yet):
#        curl -fsSL https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.sh | bash
#      Will clone the repo into ~/.claude/plugins/x265-compress-skill, then
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
#   - Defaults to $HOME/.claude/plugins/x265-compress-skill.

set -euo pipefail

REPO_URL="https://github.com/DominikStyp/x265-compress-skill.git"
# Installs as a Claude Code plugin (per .claude-plugin/plugin.json).
# Use SKILL_DIR=<path> to override.
DEFAULT_SKILL_DIR="$HOME/.claude/plugins/x265-compress-skill"

# --- Detect: are we running from inside a clone, or piped fresh? ------------
# Look for the plugin manifest as the marker — SKILL.md moved under skills/
# in the plugin restructure, but plugin.json is stable at the repo root.
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$( cd "$( dirname "$SCRIPT_PATH" )" 2>/dev/null && pwd )" || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/.claude-plugin/plugin.json" ]; then
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

# --- sudo prefix ------------------------------------------------------------
# Linux package installs need root. Wrap apt/dnf/etc. in $SUDO so the
# installer also works (a) when run as root (minimal containers where
# sudo isn't installed), and (b) for normal users (sudo prompts once).
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo "
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
            Linux)
                echo "    Install (pick one matching your distro):"
                echo "      ${SUDO}apt install git           # Debian/Ubuntu"
                echo "      ${SUDO}dnf install git           # Fedora/RHEL"
                echo "      ${SUDO}pacman -S git             # Arch"
                echo "      ${SUDO}zypper install git        # openSUSE"
                echo "      ${SUDO}apk add git               # Alpine" ;;
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
    case "$(uname -s)" in
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                echo "    Ubuntu 20.04 ships Python 3.8 by default. Install a newer Python:"
                echo "      ${SUDO}apt install -y python3.10"
                echo "      (Ubuntu 20.04 needs deadsnakes first:"
                echo "       ${SUDO}add-apt-repository -y ppa:deadsnakes/ppa && ${SUDO}apt update)"
                echo "    Or upgrade to Ubuntu 22.04+ which ships Python 3.10+,"
                echo "    or use pyenv (https://github.com/pyenv/pyenv) for per-user Python versions."
            elif command -v dnf >/dev/null 2>&1; then
                echo "    Install a newer Python: ${SUDO}dnf install -y python3.12"
                echo "      (or python3.11 — whichever your repo provides; on Fedora plain python3 is already 3.10+)"
            elif command -v pacman >/dev/null 2>&1; then
                echo "    Install: ${SUDO}pacman -S python   (Arch ships the latest already)"
            fi ;;
        Darwin)
            echo "    Install via Homebrew: brew install python  (ships current Python 3)" ;;
    esac
    exit 1
fi
info "Python $PY_VER OK"

# ---------------------------------------------------------------------------
# Step 3: ffmpeg + ffprobe
# ---------------------------------------------------------------------------
install_ffmpeg() {
    case "$(uname -s)" in
        Darwin)
            # Find brew even if its prefix isn't yet on PATH (common on
            # fresh Apple Silicon installs where the user hasn't run the
            # `brew shellenv` snippet yet).
            BREW=$(command -v brew 2>/dev/null || true)
            if [ -z "$BREW" ]; then
                for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
                    if [ -x "$candidate" ]; then
                        BREW="$candidate"
                        break
                    fi
                done
            fi
            if [ -n "$BREW" ]; then
                info "Installing ffmpeg via Homebrew ($BREW)..."
                "$BREW" install ffmpeg
                # Pull brew's bin dir onto PATH for the rest of this script
                # so the post-install ffmpeg -version check finds the binary.
                if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
                    eval "$("$BREW" shellenv)" 2>/dev/null || true
                fi
            else
                err "Homebrew not found. Install from https://brew.sh first, or install ffmpeg manually from https://ffmpeg.org/download.html"
                exit 1
            fi ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                info "Installing ffmpeg via apt..."
                ${SUDO}apt-get update && ${SUDO}apt-get install -y ffmpeg
            elif command -v dnf >/dev/null 2>&1; then
                info "Installing ffmpeg via dnf..."
                # Fedora/RHEL main repos carry only the stripped `ffmpeg-free`;
                # the full ffmpeg lives in RPM Fusion. Try the full build,
                # enabling RPM Fusion on Fedora if the first attempt fails, and
                # fall back to ffmpeg-free so the install still succeeds where
                # RPM Fusion can't be reached.
                if ! ${SUDO}dnf install -y ffmpeg >/dev/null 2>&1; then
                    fedora_ver="$(rpm -E %fedora 2>/dev/null || true)"
                    case "$fedora_ver" in
                        ''|*[!0-9]*) : ;;  # not a clean Fedora release — skip RPM Fusion
                        *) ${SUDO}dnf install -y \
                             "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-${fedora_ver}.noarch.rpm" \
                             >/dev/null 2>&1 || true ;;
                    esac
                    ${SUDO}dnf install -y ffmpeg || ${SUDO}dnf install -y ffmpeg-free
                fi
            elif command -v pacman >/dev/null 2>&1; then
                info "Installing ffmpeg via pacman..."
                ${SUDO}pacman -S --noconfirm ffmpeg
            elif command -v zypper >/dev/null 2>&1; then
                info "Installing ffmpeg via zypper..."
                ${SUDO}zypper --non-interactive install ffmpeg
            elif command -v apk >/dev/null 2>&1; then
                info "Installing ffmpeg via apk..."
                # ffmpeg is in Alpine's 'community' repo (not 'main'), and bash
                # isn't in the base image — the generated .sh needs it
                # (PIPESTATUS, read -n1). --no-cache fetches a fresh index.
                if ! ${SUDO}apk add --no-cache ffmpeg bash; then
                    err "apk could not install ffmpeg+bash. On minimal Alpine, enable the 'community' repo:"
                    echo "    uncomment the .../community line in /etc/apk/repositories, then re-run."
                    exit 1
                fi
            else
                err "No supported package manager (apt/dnf/pacman/zypper/apk). Install ffmpeg manually."
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
# Step 4b: Stamp the install with the current commit SHA. Helps bug
# reports identify the exact build (plugin.json has the published
# version; version.txt has the specific commit — useful when a user
# is on master between releases).
# ---------------------------------------------------------------------------
if [ -d "$SKILL_DIR/.git" ]; then
    git -C "$SKILL_DIR" rev-parse HEAD > "$SKILL_DIR/version.txt" 2>/dev/null || true
fi

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
echo "    See $SKILL_DIR/skills/x265-compress-skill/SKILL.md for the agent playbook,"
echo "    and $SKILL_DIR/docs/AGENT_QUEUE_RECIPES.md for queue.json templates."
