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
import wx

print(f'PyInstaller {PyInstaller.__version__}; wxPython {wx.version()} is available.')
'@
    & $PythonExecutable "-c" $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        throw "Installer builds require the Python environment supplied with -Python to have .[installer] installed (PyInstaller and wxPython)."
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

$consoleExecutable = Join-Path $applicationDirectory "twrailwaysim-console.exe"
$guiExecutable = Join-Path $applicationDirectory "twrailwaysim.exe"
foreach ($executable in @($consoleExecutable, $guiExecutable)) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce the expected executable: $executable"
    }
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
Invoke-NativeChecked $guiExecutable @("--check-gui")

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
