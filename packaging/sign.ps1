<#
.SYNOPSIS
Self-sign a built binary with this user's code-signing certificate.

.DESCRIPTION
The app ships Authenticode-signed with a self-signed certificate that
lives in the current user's store - the only kind of signing available on
this machine (a standard account cannot buy or install a CA-issued
certificate, and signtool is not present because no Windows SDK is
installed). Set-AuthenticodeSignature, which ships with PowerShell, does
the signing itself.

The certificate is created ONCE and reused for every build, found by its
fixed subject below. Regenerating it per build is the one mistake worth
spelling out: every machine that imported the previous .cer into its
Trusted People store trusts that certificate and no other, so a new cert
per build would strand each of them back at "unknown publisher" with no
way to notice.

The timestamp server is deliberately http:, not https:. On this machine
PowerShell's RFC 3161 client fails against the https endpoints of the
public timestamp servers; the http endpoint of the same servers answers.

Success is judged by reading the signature back from the signed file,
not by trusting the object Set-AuthenticodeSignature returns: the cmdlet
reports the signing it attempted, and the file is the thing that ships.
The readback's criterion is deliberately NOT Status -eq "Valid": a
self-signed certificate's chain terminates in itself, which sits in no
trusted-root store on purpose - importing it there is a trust decision
that belongs to each machine, not to a build script - so the readback
reads UnknownError ("terminated in a root certificate which is not
trusted") here and on every machine that has not imported the .cer.
That is the designed state of the shipped binaries, not a signing
failure. What the readback can and must prove is the three ways signing
actually fails: no signature on the file, the wrong certificate, or no
timestamp - the last being the case this gate exists for, because a
timestamp server that did not answer degrades Set-AuthenticodeSignature
to a warning and would otherwise ship a signature that dies with the
certificate. (Measured 2026-09-25, the release's first real signing: the
signature, the project certificate and the DigiCert timestamp were all
present and correct, and Status still read UnknownError - the first
release stopped on that, which is how the criterion was found wrong.)

Which certificate: one whose thumbprint is pinned in app/config.py
SIGNING_THUMBPRINTS, the list every installed copy's updater checks a
downloaded installer against (app/updater.py judge_signature). A build
signed by any other certificate would be refused by every install's
updater, so this script refuses to produce one. If no pinned certificate
is in this user's store - a new machine, a lost profile - it stops rather
than silently minting a new one; restore the backup (see
export-signing-cert.ps1) or, deliberately, pass -NewCert and then ship a
transition release whose pins list both thumbprints BEFORE any release is
signed by the new certificate alone.

.EXAMPLE
.\sign.ps1 -Path dist\RotmanLSMCalendar\RotmanLSMCalendar.exe
#>

param(
    [Parameter(Mandatory = $true)][string]$Path,
    # Optional: where to write the .cer other machines import to trust
    # the signature. Written per build so the shipped cert is current.
    [string]$ExportTo = "",
    # Deliberate only: create a certificate when no pinned one is present.
    # See the header - the new thumbprint must be pinned in a transition
    # release first, or every installed copy refuses the next update.
    [switch]$NewCert
)

$ErrorActionPreference = "Stop"

$Subject = "CN=Rotman LSM Calendar (self-signed code signing)"

# The pins, read from the one place the app reads them from.
$configPy = Join-Path (Split-Path $PSScriptRoot -Parent) "app\config.py"
$pinBlock = [regex]::Match((Get-Content -Raw $configPy),
    'SIGNING_THUMBPRINTS\s*=\s*\(([^)]*)\)')
if (-not $pinBlock.Success) { throw "SIGNING_THUMBPRINTS not found in $configPy" }
$pins = @([regex]::Matches($pinBlock.Groups[1].Value, '[0-9A-Fa-f]{40}') |
    ForEach-Object { $_.Value.ToUpper() })
if (-not $pins.Count) { throw "SIGNING_THUMBPRINTS in $configPy pins nothing" }

# -CodeSigningCert filters out certificates that cannot sign code.
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $pins -contains $_.Thumbprint.ToUpper() -and $_.HasPrivateKey } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1

if (-not $cert) {
    if (-not $NewCert) {
        throw ("no pinned signing certificate (" + ($pins -join ", ") +
               ") with its private key is in Cert:\CurrentUser\My. Restore it " +
               "from the PFX backup (packaging\export-signing-cert.ps1 made it), " +
               "or pass -NewCert deliberately - see this script's header.")
    }
    Write-Host "  creating code-signing certificate (current user store)..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject $Subject -HashAlgorithm SHA256 `
        -CertStoreLocation Cert:\CurrentUser\My
    Write-Host ("  created: thumbprint $($cert.Thumbprint). Add it to " +
                "SIGNING_THUMBPRINTS and ship a transition release before " +
                "any release is signed by it alone.") -ForegroundColor Yellow
}

# Deliberately NOT imported anywhere for local trust. An earlier version
# of this script imported the certificate into Trusted People believing
# that store would make the local readback read Valid; measured 2026-09-25
# it does not - the chain still terminates in a root no trust provider
# accepts, and the signature reads UnknownError with the certificate
# sitting right there in the store. Importing into Trusted Root would
# flip the readback to Valid, and is each machine's own decision (the
# shipped .cer, plus step 3 of the release notes), not the build's.

if (-not (Test-Path $Path)) { throw "cannot sign: $Path does not exist" }

Set-AuthenticodeSignature -FilePath $Path -Certificate $cert `
    -HashAlgorithm SHA256 `
    -TimestampServer "http://timestamp.digicert.com" | Out-Null

# The readback is the verdict, on the three conditions the header names.
# A signature that fails any of them does not ship from this script.
$check = Get-AuthenticodeSignature -FilePath $Path
if ($check.Status -eq "NotSigned") {
    throw ("signing $Path failed: no signature is on the file")
}
if (-not $check.SignerCertificate -or
    $check.SignerCertificate.Thumbprint -ne $cert.Thumbprint) {
    throw ("signing $Path failed: the signature is not the project certificate")
}
if (-not $check.TimeStamperCertificate) {
    throw ("signing $Path failed: the signature has no timestamp - the timestamp server did not answer")
}

Write-Host ("  signed: " + (Split-Path $Path -Leaf) +
            " (thumbprint " + $cert.Thumbprint + ", timestamped)") `
    -ForegroundColor Green

if ($ExportTo -ne "") {
    Export-Certificate -Cert $cert -FilePath $ExportTo | Out-Null
    Write-Host "  certificate exported: $ExportTo"
}