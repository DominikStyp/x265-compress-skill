# x265-compress-skill installer (Windows).
#
# Two ways to invoke:
#   1. Pipe from web (fresh machine, no clone yet):
#        irm https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.ps1 | iex
#      Will clone the repo into %USERPROFILE%\.claude\skills\ffmpeg-compress-video,
#      then verify deps and run a smoke test.
#
#   2. Inside an existing clone (re-run after git pull, etc.):
#        powershell -ExecutionPolicy Bypass -File install.ps1
#      Skips the clone step.
#
# Non-interactive mode (for CI / automation):
#   - $env:INSTALL_YES=1 ; irm ... | iex
#   - or for the file form, pass -Yes.

[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/DominikStyp/x265-compress-skill.git"
# Installs as a Claude Code plugin (per .claude-plugin/plugin.json).
# Use $env:SKILL_DIR=<path> to override.
$DefaultSkillDir = "$env:USERPROFILE\.claude\plugins\ffmpeg-compress-video"

# --- Detect: are we running from inside a clone, or piped fresh? ------------
# Look for the plugin manifest as the marker - SKILL.md moved under skills/
# in the plugin restructure, but plugin.json is stable at the repo root.
$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = if ($ScriptPath) { Split-Path -Parent $ScriptPath } else { $null }
if ($ScriptDir -and (Test-Path "$ScriptDir\.claude-plugin\plugin.json")) {
    $SkillDir = $ScriptDir
    $NeedsClone = $false
} else {
    $SkillDir = if ($env:SKILL_DIR) { $env:SKILL_DIR } else { $DefaultSkillDir }
    $NeedsClone = $true
}

# --- Non-interactive flag ---------------------------------------------------
$AutoYes = $Yes -or ($env:INSTALL_YES -eq "1") -or ($env:CI -eq "1")

function Write-Info($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Write-Err2($msg)  { Write-Host "ERR $msg" -ForegroundColor Red }

function Confirm-Prompt($question) {
    if ($AutoYes) { return $true }
    try {
        $ans = Read-Host "    $question [Y/n]"
        return ($ans -notmatch '^[Nn]')
    } catch {
        Write-Warn "no interactive console and no -Yes flag; assuming 'no'"
        return $false
    }
}

# ---------------------------------------------------------------------------
# Step 0: git (only needed for bootstrap)
# ---------------------------------------------------------------------------
if ($NeedsClone) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Err2 "git not found on PATH - required to clone the repo."
        Write-Host "    Install: winget install --id Git.Git   (or from https://git-scm.com/download/win)"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 1: Clone (only when bootstrapping)
# ---------------------------------------------------------------------------
if ($NeedsClone) {
    if (Test-Path "$SkillDir\.git") {
        Write-Info "Repo already at $SkillDir - pulling latest"
        try {
            Push-Location $SkillDir
            git pull --ff-only
        } finally {
            Pop-Location
        }
    } elseif (Test-Path $SkillDir) {
        Write-Err2 "$SkillDir exists and is not a git clone - refusing to overwrite."
        Write-Host "    Move/delete it first, or set `$env:SKILL_DIR to a different location."
        exit 1
    } else {
        Write-Info "Cloning $RepoUrl to $SkillDir"
        $parent = Split-Path -Parent $SkillDir
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        git clone --depth 1 $RepoUrl $SkillDir
    }
}

# ---------------------------------------------------------------------------
# Step 2: Python 3.9+
# ---------------------------------------------------------------------------
Write-Info "Checking Python..."
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Err2 "python not found on PATH."
    Write-Host "    Install from https://www.python.org/downloads/  (check 'Add to PATH' in the installer)"
    Write-Host "    Or: winget install --id Python.Python.3.12"
    exit 1
}
$pyVerOutput = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
$parts = $pyVerOutput -split '\.'
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 9)) {
    Write-Err2 "Python 3.9+ required, found $pyVerOutput."
    exit 1
}
Write-Info "Python $pyVerOutput OK"

# ---------------------------------------------------------------------------
# Step 3: ffmpeg + ffprobe
# ---------------------------------------------------------------------------
function Install-FFmpeg {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Err2 "winget not available. Install ffmpeg manually from https://ffmpeg.org/download.html and ensure ffmpeg.exe + ffprobe.exe are on PATH."
        exit 1
    }
    Write-Info "Installing ffmpeg via winget (Gyan.FFmpeg)..."
    & winget install --id Gyan.FFmpeg --silent --scope user `
        --accept-package-agreements --accept-source-agreements
    # winget --scope user puts shims under %LOCALAPPDATA%\Microsoft\WinGet\Links
    # which is on PATH after the next shell start. For this session, refresh
    # PATH from the registry so the post-install verify finds ffmpeg.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','User') + `
                ';' + [System.Environment]::GetEnvironmentVariable('Path','Machine')
}

Write-Info "Checking ffmpeg + ffprobe..."
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffmpeg -or -not $ffprobe) {
    Write-Warn "ffmpeg/ffprobe not on PATH."
    if (Confirm-Prompt "Install ffmpeg now via winget?") {
        Install-FFmpeg
        $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if (-not $ffmpeg) {
            Write-Warn "ffmpeg installed but not yet on PATH for this shell session."
            Write-Warn "Open a new PowerShell window and re-run this installer to verify."
            exit 0
        }
    } else {
        Write-Err2 "ffmpeg required. Install it and re-run this installer."
        exit 1
    }
}
$ffmpegVer = (& ffmpeg -version | Select-Object -First 1)
Write-Info "$ffmpegVer"

# ---------------------------------------------------------------------------
# Step 4: Smoke test the skill's import graph
# ---------------------------------------------------------------------------
Write-Info "Verifying skill imports..."
$env:PYTHONIOENCODING = "utf-8"
& python "$SkillDir\_smoke_test.py" $SkillDir

# ---------------------------------------------------------------------------
# Step 5: Where it lives + next steps
# ---------------------------------------------------------------------------
Write-Info "Installation complete."
Write-Host ""
Write-Host "    Skill location:  $SkillDir"
Write-Host ""
Write-Host "    The skill activates automatically in Claude Code on prompts like:"
Write-Host "      'compress this video'"
Write-Host "      'shrink this mp4'"
Write-Host "      'compress all videos in <folder>'"
Write-Host ""
Write-Host "    Standalone (no Claude Code):"
Write-Host ('      python "' + $SkillDir + '\compress.py" "C:\path\to\video.mp4" --resumable')
Write-Host ""
Write-Host ("    See " + $SkillDir + "\skills\ffmpeg-compress-video\SKILL.md for the agent playbook,")
Write-Host ("    and " + $SkillDir + "\docs\AGENT_QUEUE_RECIPES.md for queue.json templates.")
