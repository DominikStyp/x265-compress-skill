# x265-compress-skill installer (Windows).
#
# Idempotent — safe to re-run. Checks Python + ffmpeg + ffprobe, offers
# to install ffmpeg via winget if missing, runs a smoke import test,
# prints next steps.
#
# Auto-yes mode: pass -Yes to accept every prompt.

[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$SkillDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Info($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Write-Err2($msg)  { Write-Host "ERR $msg" -ForegroundColor Red }

function Confirm-Prompt($question) {
    if ($Yes) { return $true }
    $ans = Read-Host "    $question [Y/n]"
    return ($ans -notmatch '^[Nn]')
}

# ---------------------------------------------------------------------------
# Step 1: Python 3.9+
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
# Step 2: ffmpeg + ffprobe
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
    # winget --scope user puts shims in %LOCALAPPDATA%\Microsoft\WinGet\Links
    # which is on PATH after the next shell start. For this session, refresh
    # PATH from registry so the post-install verify can find ffmpeg.
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
        # Re-check after install
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
# Step 3: Smoke test the skill's import graph
# ---------------------------------------------------------------------------
Write-Info "Verifying skill imports..."
$env:PYTHONIOENCODING = "utf-8"
& python -c @"
import sys
sys.path.insert(0, r'$SkillDir')
import platform_compat as pc
from compress_modules import probe, plan, script_writer
from encode_modules import encoder, display
from queue_modules import job_runner, job_schema, queue_io
print('  OS detected:', pc.os_name())
print('  Script extension:', script_writer.SCRIPT_EXTENSION)
"@

# ---------------------------------------------------------------------------
# Step 4: Where it lives + next steps
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
Write-Host "      python `"$SkillDir\compress.py`" `"C:\path\to\video.mp4`" --resumable"
Write-Host ""
Write-Host "    See $SkillDir\SKILL.md for the agent playbook,"
Write-Host "    and $SkillDir\docs\AGENT_QUEUE_RECIPES.md for queue.json templates."
