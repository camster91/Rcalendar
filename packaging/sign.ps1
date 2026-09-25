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

.EXAMPLE
.\sign.ps1 -Path dist\RotmanLSMCalendar\RotmanLSMCalendar.exe
#>

param(
    [Parameter(Mandatory = $true)][string]$Path,
    # Optional: where to write the .cer other machines import to trust
    # the signature. Written per build so the shipped cert is current.
    [string]$ExportTo = ""
)

$ErrorActionPreference = "Stop"

$Subject = "CN=Rotman LSM Calendar (self-signed code signing)"

# Find-or-create, by subject. -CodeSigningCert filters out certificates
# that cannot sign code, which keeps a same-named TLS cert from being
# silently picked up if one ever appears in this store.
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq $Subject } |
    Select-Object -First 1

if (-not $cert) {
    Write-Host "  creating code-signing certificate (current user store)..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject $Subject -HashAlgorithm SHA256 `
        -CertStoreLocation Cert:\CurrentUser\My
    Write-Host "  created: thumbprint $($cert.Thumbprint)" -ForegroundColor Green
}

# Local trust, idempotently. Without this, Windows reads the signature
# back as untrusted on this very machine, and the first execution prompt
# says "unknown publisher" over a signature that is sitting right there
# in the user's own store. Trusted People (not Trusted Root) is the
# minimal store for exactly this shape: "I trust this publisher, and I am
# not asking anyone else to".
$trusted = Get-ChildItem Cert:\CurrentUser\TrustedPeople |
    Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
if (-not $trusted) {
    $tmp = Join-Path $env:TEMP ("lsm-sign-" + $cert.Thumbprint + ".cer")
    Export-Certificate -Cert $cert -FilePath $tmp | Out-Null
    Import-Certificate -FilePath $tmp `
        -CertStoreLocation Cert:\CurrentUser\TrustedPeople | Out-Null
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Host "  imported into Cert:\CurrentUser\TrustedPeople (this machine now trusts it)"
}

if (-not (Test-Path $Path)) { throw "cannot sign: $Path does not exist" }

Set-AuthenticodeSignature -FilePath $Path -Certificate $cert `
    -HashAlgorithm SHA256 `
    -TimestampServer "http://timestamp.digicert.com" | Out-Null

# The readback is the verdict. Set-AuthenticodeSignature can return
# without throwing while leaving a signature Windows will not validate
# (a timestamp server that did not answer degrades to a warning), so the
# check is on the file, where the thing that ships actually is.
$check = Get-AuthenticodeSignature -FilePath $Path
if ($check.Status -ne "Valid") {
    throw ("signing $Path failed: " + $check.Status + " - " + $check.StatusMessage)
}

Write-Host ("  signed: " + (Split-Path $Path -Leaf) +
            " (thumbprint " + $cert.Thumbprint + ", " + $check.Status + ")") `
    -ForegroundColor Green

if ($ExportTo -ne "") {
    Export-Certificate -Cert $cert -FilePath $ExportTo | Out-Null
    Write-Host "  certificate exported: $ExportTo"
}