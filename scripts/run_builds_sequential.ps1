#requires -Version 5.1

[CmdletBinding()]
param(
    [string[]]$Heroes = @("Karnok", "Mak", "Dooley", "Vanessa", "Pygmalien", "Jules", "Stelle"),
    [string]$Python = "",
    [string]$StateFile = "pipeline_state.json",
    [string]$TrackerRepo = "..\bazaar_tracker",
    [string]$StatsDir = ".\stats",
    [string]$OutputDir = ".\artifacts",
    [ValidateSet("deterministic", "no_llm_shadow")]
    [string]$ClassifierMode = "deterministic",
    [int]$BazaarDbRetries = 2,
    [switch]$NoBazaarDb
)

$ErrorActionPreference = "Stop"

function Resolve-PythonCommand {
    param([string]$RequestedPython)

    if ($RequestedPython) {
        & $RequestedPython --version | Out-Host
        return [pscustomobject]@{
            Command = $RequestedPython
            PrefixArgs = @()
        }
    }

    foreach ($candidate in @("python", "python3")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        & $command.Source --version | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{
                Command = $command.Source
                PrefixArgs = @()
            }
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3.12 --version | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{
                Command = $py.Source
                PrefixArgs = @("-3.12")
            }
        }
    }

    throw "Could not find a working Python. Pass -Python with a Python 3.12 executable path."
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repoRoot
try {
    $pythonSpec = Resolve-PythonCommand -RequestedPython $Python
    $total = $Heroes.Count

    for ($index = 0; $index -lt $total; $index++) {
        $hero = $Heroes[$index]
        Write-Host ""
        Write-Host "=== [$($index + 1)/$total] Running $hero ==="

        $pipelineArgs = @(
            "-m", "automated_builds_pipeline.pipeline", "run",
            "--hero", $hero,
            "--state-file", $StateFile,
            "--tracker-repo", $TrackerRepo,
            "--stats-dir", $StatsDir,
            "--output-dir", $OutputDir,
            "--classifier-mode", $ClassifierMode,
            "--bazaardb-retries", "$BazaarDbRetries"
        )
        if ($NoBazaarDb) {
            $pipelineArgs += "--no-bazaardb"
        }

        & $pythonSpec.Command @($pythonSpec.PrefixArgs + $pipelineArgs)
        if ($LASTEXITCODE -ne 0) {
            throw "$hero failed with exit code $LASTEXITCODE; stopping before the next hero."
        }

        Write-Host "=== Finished $hero ==="
    }
}
finally {
    Pop-Location
}
