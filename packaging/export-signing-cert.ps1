<#
.SYNOPSIS
Back up the code-signing certificate AND its private key to a
password-protected .pfx file.

.DESCRIPTION
Every installed copy of the app trusts updates signed by the certificate
pinned in app/config.py SIGNING_THUMBPRINTS, and nothing else. The private
key exists in exactly one place: this Windows account's certificate store
on this machine. Lose the profile or the machine and no update can ever be
signed that the installed copies will accept - every office install would
have to be updated by hand.

This writes the certificate with its key to a .pfx you choose, protected by
a password you type (it is never echoed, stored or logged). Keep the file
somewhere backed up that is NOT the public releases repo - OneDrive or a
password manager's file attachment - and the password somewhere else.

Restore on a new machine or profile with:
  Import-PfxCertificate -FilePath <file.pfx> -CertStoreLocation Cert:\CurrentUser\My -Password (Read-Host -AsSecureString)

.EXAMPLE
.\export-signing-cert.ps1 -OutFile "$env:OneDrive\Backups\rotman-lsm-signing.pfx"
#>

param(
    [Parameter(Mandatory = $true)][string]$OutFile
)

$ErrorActionPreference = "Stop"

$configPy = Join-Path (Split-Path $PSScriptRoot -Parent) "app\config.py"
$pinBlock = [regex]::Match((Get-Content -Raw $configPy),
    'SIGNING_THUMBPRINTS\s*=\s*\(([^)]*)\)')
if (-not $pinBlock.Success) { throw "SIGNING_THUMBPRINTS not found in $configPy" }
$pins = @([regex]::Matches($pinBlock.Groups[1].Value, '[0-9A-Fa-f]{40}') |
    ForEach-Object { $_.Value.ToUpper() })

$certs = @(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $pins -contains $_.Thumbprint.ToUpper() -and $_.HasPrivateKey })
if (-not $certs.Count) {
    throw ("no pinned signing certificate with its private key is in " +
           "Cert:\CurrentUser\My - nothing to back up")
}
if (Test-Path $OutFile) { throw "$OutFile already exists - choose a new name" }

$password = Read-Host -AsSecureString "Password for the .pfx (not echoed)"
$confirm = Read-Host -AsSecureString "Type it again"
$plainA = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password))
$plainB = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($confirm))
$same = ($plainA -ceq $plainB)
$long = ($plainA.Length -ge 12)
$plainA = $null; $plainB = $null
if (-not $same) { throw "the passwords did not match - nothing written" }
if (-not $long) { throw "use at least 12 characters - nothing written" }

foreach ($cert in $certs) {
    $target = $OutFile
    if ($certs.Count -gt 1) {
        $target = [IO.Path]::ChangeExtension($OutFile,
            $cert.Thumbprint.Substring(0, 8) + ".pfx")
    }
    Export-PfxCertificate -Cert $cert -FilePath $target -Password $password `
        -ChainOption EndEntityCertOnly | Out-Null
    # Prove the file opens with the password and holds the key.
    $check = Get-PfxData -FilePath $target -Password $password
    if ($check.EndEntityCertificates[0].Thumbprint -ne $cert.Thumbprint) {
        throw "$target did not read back as the signing certificate"
    }
    Write-Host ("  backed up: " + $target + " (thumbprint " + $cert.Thumbprint +
                ", expires " + $cert.NotAfter.ToString("yyyy-MM-dd") + ")") `
        -ForegroundColor Green
}
