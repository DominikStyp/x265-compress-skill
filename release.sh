#!/usr/bin/env bash
# Automate steps 4-7 of the release process documented in RELEASING.md.
#
# Usage:
#   bash release.sh <VERSION> "<SUMMARY>"
#
# Example:
#   bash release.sh 1.2.0 "Bug fixes + Linux Wayland support"
#
# Pre-flight checks before tagging/pushing:
#   1. CHANGELOG.md must contain a `## [VERSION]` section
#   2. .claude-plugin/plugin.json `version` must equal VERSION
#   3. Tag `vVERSION` must NOT already exist
#   4. Working tree must be clean (no uncommitted changes)
#
# If any check fails, nothing is created or pushed — the script exits
# non-zero and prints a clear message.

set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "usage: $0 <VERSION> [\"<SUMMARY>\"]" >&2
    echo "example: $0 1.2.0 \"Bug fixes + Linux Wayland support\"" >&2
    exit 1
fi

VERSION="$1"
SUMMARY="${2:-Release v${VERSION}}"
TAG="v${VERSION}"

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO_ROOT"

C_GREEN='\033[32;1m'
C_RED='\033[31;1m'
C_YELLOW='\033[33;1m'
C_RESET='\033[0m'
info() { printf "${C_GREEN}==>${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!!${C_RESET}  %s\n" "$*"; }
err()  { printf "${C_RED}ERR${C_RESET} %s\n" "$*" >&2; }

# --- Pre-flight checks ------------------------------------------------------

# 1. CHANGELOG.md must contain ## [VERSION]
if ! grep -qE "^## \[${VERSION}\]" CHANGELOG.md 2>/dev/null; then
    err "CHANGELOG.md has no \`## [${VERSION}]\` section."
    err "Add one before running this script (see RELEASING.md step 2)."
    exit 1
fi

# 2. plugin.json version must match
MANIFEST_VERSION=$(python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])")
if [ "$MANIFEST_VERSION" != "$VERSION" ]; then
    err "plugin.json version (${MANIFEST_VERSION}) doesn't match ${VERSION}."
    err "Bump it first (see RELEASING.md step 1)."
    exit 1
fi

# 3. Tag must not already exist
if git rev-parse "${TAG}" >/dev/null 2>&1; then
    err "Tag ${TAG} already exists. Delete it first if you really mean to re-release."
    err "  git tag -d ${TAG} && git push --delete origin ${TAG}"
    exit 1
fi

# 4. Working tree must be clean
if [ -n "$(git status --porcelain)" ]; then
    err "Working tree has uncommitted changes. Commit the version bump"
    err "+ changelog entry first (see RELEASING.md step 3)."
    git status --short >&2
    exit 1
fi

info "Pre-flight OK. CHANGELOG has [${VERSION}]; plugin.json matches; tag is fresh; tree clean."

# --- Step 4: create + push tag ----------------------------------------------

info "Creating annotated tag ${TAG}..."
git tag -a "${TAG}" -m "${TAG} — ${SUMMARY}"
git push origin "${TAG}"

# --- Step 5: extract release notes ------------------------------------------

NOTES_FILE=".release-notes-${TAG}.md"
info "Extracting [${VERSION}] section from CHANGELOG to ${NOTES_FILE}..."
awk -v ver="${VERSION}" '
    $0 ~ "^## \\[" ver "\\]" { flag=1; next }
    flag && /^## \[/         { exit }
    flag                      { print }
' CHANGELOG.md > "${NOTES_FILE}"

if [ ! -s "${NOTES_FILE}" ]; then
    err "Extracted notes are empty — CHANGELOG.md section may be malformed."
    err "Investigate ${NOTES_FILE} before publishing."
    exit 1
fi

# --- Step 6: create GitHub release ------------------------------------------

if ! command -v gh >/dev/null 2>&1; then
    # Try the Windows winget shim location
    GH="$HOME/AppData/Local/Microsoft/WinGet/Links/gh.exe"
    if [ ! -x "$GH" ]; then
        err "gh CLI not found. Install via https://cli.github.com/ then re-run."
        exit 1
    fi
else
    GH="gh"
fi

info "Creating GitHub release ${TAG}..."
"$GH" release create "${TAG}" \
    --title "${TAG} — ${SUMMARY}" \
    --notes-file "${NOTES_FILE}" \
    --latest

# --- Step 7: cleanup --------------------------------------------------------

rm -f "${NOTES_FILE}"
info "Done. Release published at https://github.com/DominikStyp/x265-compress-skill/releases/tag/${TAG}"
