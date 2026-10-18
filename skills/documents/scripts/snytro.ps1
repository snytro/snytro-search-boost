# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
$ErrorActionPreference = 'Stop'
if (-not $env:SNYTRO_DATA_DIR) { $env:SNYTRO_DATA_DIR = Join-Path $env:LOCALAPPDATA 'SnytroCLI' }
$Python = Join-Path $env:SNYTRO_DATA_DIR 'runtime/.venv/Scripts/python.exe'
if (Test-Path $Python) { & $Python -c 'import snytro_cli'; $MissingCli = $LASTEXITCODE -ne 0 }
else { $MissingCli = $true }
if ($MissingCli) { & (Join-Path $PSScriptRoot 'bootstrap.ps1') }
& $Python -m snytro_cli.cli @args
exit $LASTEXITCODE
