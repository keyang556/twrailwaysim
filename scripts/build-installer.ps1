[CmdletBinding()]
param(
    [string]$Version,
    [string]$Python = "python",
    [string]$InnoCompiler,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Assert-InstallerDependencies {
    param([Parameter(Mandatory = $true)][string]$PythonExecutable)

    $dependencyCheck = @'
import PyInstaller
import pygame
import wx

print(
    f'PyInstaller {PyInstaller.__version__}; wxPython {wx.version()}; '
    f'pygame {pygame.version.ver} are available.'
)
'@
    & $PythonExecutable "-c" $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        throw "Installer builds require the Python environment supplied with -Python to have .[installer] installed (PyInstaller, wxPython, and pygame)."
    }
}

function Get-ProjectVersion {
    param([Parameter(Mandatory = $true)][string]$ProjectFile)

    $contents = Get-Content -Raw -Encoding utf8 $ProjectFile
    $match = [regex]::Match($contents, '(?m)^version\s*=\s*"([^"]+)"\s*$')
    if (-not $match.Success) {
        throw "Could not read the project version from $ProjectFile."
    }

    return $match.Groups[1].Value
}

function Get-InnoCompiler {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    foreach ($programFiles in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $programFiles) {
            continue
        }

        foreach ($innoDirectory in @("Inno Setup 7", "Inno Setup 6")) {
            $candidate = Join-Path $programFiles "$innoDirectory\ISCC.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
    }

    throw "Inno Setup was not found. Install it, then run this script again."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectFile = Join-Path $projectRoot "pyproject.toml"
$specFile = Join-Path $projectRoot "packaging\twrailwaysim.spec"
$installerScript = Join-Path $projectRoot "installer\twrailwaysim.iss"
$distDirectory = Join-Path $projectRoot "dist"
$workDirectory = Join-Path $projectRoot "build\pyinstaller"
$applicationDirectory = Join-Path $distDirectory "twrailwaysim"
$installerOutputDirectory = Join-Path $distDirectory "installer"
# PyInstaller 6's onedir layout stores application data beneath _internal.
$applicationContentDirectory = Join-Path $applicationDirectory "_internal"
$bundledNvdaControllerClient = Join-Path $applicationContentDirectory "railway_sim\lib\nvdaControllerClient.dll"
$sourceNvdaControllerClient = Join-Path $projectRoot "third_party\nvda-controller-client\2026.1.1\nvdaControllerClient.dll"
$bundledNvdaControllerLicense = Join-Path $applicationContentDirectory "third_party_licenses\nvda-controller-client\license.txt"

foreach ($requiredPath in @($projectFile, $specFile, $installerScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required build input was not found: $requiredPath"
    }
}

$projectVersion = Get-ProjectVersion $projectFile
$resolvedVersion = if ($Version) { $Version.TrimStart("v") } else { $projectVersion }
if ($resolvedVersion -notmatch '^\d+(\.\d+){1,3}$') {
    throw "Installer versions must be numeric dot-separated values, such as 0.1.0. Received: $resolvedVersion"
}
if ($resolvedVersion -ne $projectVersion) {
    throw "The requested installer version ($resolvedVersion) does not match pyproject.toml ($projectVersion)."
}

Assert-InstallerDependencies -PythonExecutable $Python

New-Item -ItemType Directory -Force -Path $installerOutputDirectory | Out-Null

Invoke-NativeChecked $Python @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath",
    $distDirectory,
    "--workpath",
    $workDirectory,
    $specFile
)

$pyInstallerWarningFile = Join-Path $workDirectory "twrailwaysim\warn-twrailwaysim.txt"
if (-not (Test-Path -LiteralPath $pyInstallerWarningFile -PathType Leaf)) {
    throw "PyInstaller did not produce its dependency warning report: $pyInstallerWarningFile"
}
$missingWxWarnings = @(Select-String -LiteralPath $pyInstallerWarningFile -Pattern "missing module named wx" -SimpleMatch)
if ($missingWxWarnings.Count -gt 0) {
    throw "PyInstaller reported a missing wx module. Recreate the build environment with .[installer] before packaging a release."
}
$knownOptionalPygameWarnings =
    "missing module named '?pygame\.(?:_common|overlay|cdrom)'?"
$missingAudioWarnings = @(
    Select-String -LiteralPath $pyInstallerWarningFile -Pattern 'missing module named [''"]?(pygame|sdl2?)([.''"]|$)' |
        Where-Object { $_.Line -notmatch $knownOptionalPygameWarnings }
)
if ($missingAudioWarnings.Count -gt 0) {
    throw "PyInstaller reported a missing pygame or SDL module. Recreate the build environment with .[installer] before packaging a release."
}

$consoleExecutable = Join-Path $applicationDirectory "twrailwaysim-console.exe"
$guiExecutable = Join-Path $applicationDirectory "twrailwaysim.exe"
foreach ($executable in @($consoleExecutable, $guiExecutable)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce the expected executable: $executable"
    }
}

# The Controller Client is an application dependency, not something we search
# for in an NVDA installation. Verify both its precise destination and its
# provenance before producing a portable directory or installer.
if (-not (Test-Path -LiteralPath $sourceNvdaControllerClient -PathType Leaf)) {
    throw "Reviewed NVDA Controller Client source file is missing: $sourceNvdaControllerClient"
}
foreach ($requiredFile in @($bundledNvdaControllerClient, $bundledNvdaControllerLicense)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "PyInstaller did not produce the required NVDA Controller Client file: $requiredFile"
    }
}
$bundledControllerClients = @(Get-ChildItem -LiteralPath $applicationDirectory -Recurse -File -Filter "nvdaControllerClient.dll")
if ($bundledControllerClients.Count -ne 1 -or $bundledControllerClients[0].FullName -ne (Resolve-Path -LiteralPath $bundledNvdaControllerClient).Path) {
    throw "Portable build must contain exactly one nvdaControllerClient.dll at railway_sim\\lib."
}
if ((Get-FileHash -LiteralPath $sourceNvdaControllerClient -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $bundledNvdaControllerClient -Algorithm SHA256).Hash) {
    throw "The bundled NVDA Controller Client does not match the reviewed source artifact."
}

# Exercise all non-interactive commands after freezing so the build fails when
# data files, the entry point, or bundled imports are missing.
$reportedVersion = (& $consoleExecutable "--version" | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Version smoke test failed with exit code ${LASTEXITCODE}."
}
if ($reportedVersion -ne "railway-sim $projectVersion") {
    throw "The application reports '$reportedVersion', but pyproject.toml declares '$projectVersion'."
}
Invoke-NativeChecked $consoleExecutable @("--check")
Invoke-NativeChecked $consoleExecutable @("--list-scenarios")
Invoke-NativeChecked $consoleExecutable @("--check-nvda-controller")
Invoke-NativeChecked $guiExecutable @("--check-gui")

# GitHub Actions runners have no physical output device. SDL's dummy driver
# still exercises the frozen pygame/SDL_mixer import and Ogg decoder, without
# allowing a runner-provided ffplay binary to mask a missing bundled backend.
$hadSdlAudioDriver = Test-Path Env:SDL_AUDIODRIVER
$previousSdlAudioDriver = $env:SDL_AUDIODRIVER
try {
    $env:SDL_AUDIODRIVER = "dummy"
    Invoke-NativeChecked $consoleExecutable @("--check-audio")
}
finally {
    if ($hadSdlAudioDriver) {
        $env:SDL_AUDIODRIVER = $previousSdlAudioDriver
    }
    else {
        Remove-Item Env:SDL_AUDIODRIVER
    }
}

if ($SkipInstaller) {
    Write-Host "Portable application created: $applicationDirectory"
    return
}

$innoCompiler = if ($InnoCompiler) {
    if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
        throw "The supplied Inno Setup compiler was not found: $InnoCompiler"
    }
    (Resolve-Path -LiteralPath $InnoCompiler).Path
} else {
    Get-InnoCompiler
}
Invoke-NativeChecked $innoCompiler @(
    "/DAppVersion=$resolvedVersion",
    "/DSourceDir=$applicationDirectory",
    "/DOutputDir=$installerOutputDirectory",
    $installerScript
)

$installerPath = Join-Path $installerOutputDirectory "twrailwaysim-setup-$resolvedVersion.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Inno Setup did not produce the expected installer: $installerPath"
}

Write-Host "Installer created: $installerPath"
