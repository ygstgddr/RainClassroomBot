param(
    [string]$Compiler = '',
    [string]$SourceDir = (Join-Path $PSScriptRoot 'dist\RainClassroomBot'),
    [string]$OutputDir = (Join-Path $PSScriptRoot 'dist\installer')
)
$ErrorActionPreference = 'Stop'
if (-not $Compiler) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $Compiler = $command.Source }
    else {
        foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if (Test-Path -LiteralPath $candidate) { $Compiler = $candidate; break }
        }
    }
}
if (-not $Compiler -or -not (Test-Path -LiteralPath $Compiler)) { throw 'Install Inno Setup 6.7+ or supply -Compiler <path-to-ISCC.exe>.' }
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
foreach ($required in @('RainClassroomBot.exe','VERSION.txt','config\config.yaml','_internal\gui\web\app.css','docs','third_party_licenses')) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $required))) { throw "Missing build content: $required. Run build.ps1 first." }
}
$version = (Get-Content -LiteralPath (Join-Path $SourceDir 'VERSION.txt') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version' }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
& $Compiler "/DSourceDir=$SourceDir" "/DOutputDir=$OutputDir" "/DAppVersion=$version" (Join-Path $PSScriptRoot 'installer\RainClassroomBot.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$result = Join-Path $OutputDir "RainClassroomBot-$version-Windows-x64-Setup.exe"
$hash = (Get-FileHash -LiteralPath $result -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($result))" | Set-Content -LiteralPath ($result + '.sha256') -Encoding ascii
# Publish this standard manifest alongside Setup for the in-app updater.
"$hash  $([IO.Path]::GetFileName($result))" | Set-Content -LiteralPath (Join-Path $OutputDir "SHA256SUMS-$version.txt") -Encoding ascii
Write-Output "Installer ready: $result"
