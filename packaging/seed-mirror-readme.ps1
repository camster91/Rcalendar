<#
.SYNOPSIS
Seed the public releases mirror's README - once, idempotently.

.DESCRIPTION
The mirror (camster91/rotman-lsm-calendar-releases) holds nothing but
release artifacts; its README (packaging\mirror-readme.md is the source of
the text) is the one deliberate exception, and it has to exist before
release-local.ps1 can publish to the mirror: `gh release create --target
main` needs the repo to have a commit for the tag to name, and an empty
repo has none. Seeding via the contents API creates the default branch in
the same stroke.

Idempotent: if the README already exists, this script says so and exits 0
without touching it, so re-running it on a machine that already has the
mirror is harmless.

Run:  powershell -File packaging\seed-mirror-readme.ps1
(From the repo root; pure-ASCII, PS 5.1 safe.)
#>

$ErrorActionPreference = "Stop"
$Mirror = "camster91/rotman-lsm-calendar-releases"
$Src = Join-Path $PSScriptRoot "mirror-readme.md"
if (-not (Test-Path $Src)) { throw "packaging\mirror-readme.md not found" }

# Does the README already exist? A 200 means yes; anything else means no.
# 'Continue' around this one call: under 'Stop', PowerShell 5.1 turns a
# native command's redirected stderr into a terminating error, so the 404
# for a missing README - the very case this script exists for - threw
# here instead of reaching the seeding below. $LASTEXITCODE is the verdict.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $existing = gh api "repos/$Mirror/contents/README.md" --jq ".sha" 2>$null
} finally {
    $ErrorActionPreference = $prevEap
}
if ($LASTEXITCODE -eq 0 -and $existing) {
    Write-Host "mirror README already seeded (sha $existing) - nothing to do"
    exit 0
}

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Src))
gh api -X PUT "repos/$Mirror/contents/README.md" `
    -f message="The mirror's only file: what this repository is" `
    -f content=$b64 --jq ".commit.sha"
if ($LASTEXITCODE -ne 0) { throw "seeding the mirror README failed" }

# The default branch this commit created is what release-local.ps1's
# `--target main` names; print it so a mismatch is visible now, not at
# publish time.
gh repo view $Mirror --json defaultBranchRef --jq ".defaultBranchRef.name"