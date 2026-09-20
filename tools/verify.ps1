# Run everything that can be checked on one machine, in the order that fails
# fastest. Written for Windows PowerShell 5.1.
#
#     .\tools\verify.ps1
#
# Nothing here needs arguments and nothing is left behind. The exit code is 0
# only if every stage passed, so this is usable as one step in a hook or a
# build. Stages that need node are skipped with a note when node is absent
# rather than counted as failures; none of them is needed to play or build the
# game.
#
# This directory is not imported by the application and can be deleted before a
# release.

$ErrorActionPreference = "Continue"

$failed = @()
$skipped = @()

function Invoke-Stage {
    param(
        [string] $Name,
        [scriptblock] $Body
    )

    Write-Host ""
    Write-Host "== $Name ==" -ForegroundColor Cyan

    & $Body

    if ($LASTEXITCODE -ne 0) {
        $script:failed += $Name
        Write-Host "FAILED: $Name" -ForegroundColor Red
    }
}

function Test-Node {
    $found = Get-Command node -ErrorAction SilentlyContinue
    return [bool] $found
}

# The suite first. It is the fastest stage and it catches most of what the
# slower ones would catch, so a failure here saves several minutes.
Invoke-Stage "Test suite" { python -m pytest -q }

# The two repository rules. Cheap, and a failure here blocks a commit rather
# than a run, so it is worth knowing before anything slower starts.
Invoke-Stage "Repository rules" { python tools\check_ascii.py }

# Style. Expected to report nothing at all, so anything it prints is new.
Invoke-Stage "Lint" { ruff check . }

if (Test-Node) {
    Invoke-Stage "Board window arithmetic" { node tools\check_view.mjs }
    Invoke-Stage "Palettes and themes" { node tools\check_palettes.mjs }
    Invoke-Stage "Editor against validator" { python tools\check_rules_ui.py }
    Invoke-Stage "The editor in a real DOM" { node tools\check_editor.mjs }
} else {
    $skipped += "the four checks that need node"
    Write-Host ""
    Write-Host "node was not found, so four checks were skipped." -ForegroundColor Yellow
}

# Last, because it starts three copies of the game and plays a match through
# them. Several bugs in this project passed everything above and were caught
# only here.
Invoke-Stage "Three instances, a whole match" { python tools\check_match.py }

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) stage(s) failed:" -ForegroundColor Red
    foreach ($name in $failed) {
        Write-Host "  - $name"
    }
    exit 1
}

Write-Host "Everything passed." -ForegroundColor Green
foreach ($note in $skipped) {
    Write-Host "Skipped: $note"
}
exit 0
