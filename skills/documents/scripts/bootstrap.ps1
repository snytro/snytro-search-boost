# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
param([switch]$Embeddings, [switch]$Postgres)
$ErrorActionPreference = 'Stop'
$PackageDir = Split-Path $PSScriptRoot -Parent
if (-not $env:SNYTRO_DATA_DIR) { $env:SNYTRO_DATA_DIR = Join-Path $env:LOCALAPPDATA 'SnytroCLI' }
$RuntimeDir = Join-Path $env:SNYTRO_DATA_DIR 'runtime'
$ToolsDir = Join-Path $env:SNYTRO_DATA_DIR 'tools'
New-Item -ItemType Directory -Force -Path $RuntimeDir, $ToolsDir | Out-Null
$env:UV_CACHE_DIR = Join-Path $RuntimeDir 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeDir 'python'
$env:UV_PYTHON_INSTALL_BIN = '0'
$Uv = Join-Path $ToolsDir 'uv.exe'
if (-not (Test-Path $Uv)) {
    $ExistingUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($ExistingUv) { $Uv = $ExistingUv.Source }
    else {
        $Architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        $Target = switch ($Architecture) { 'X64' {'x86_64-pc-windows-msvc'} 'Arm64' {'aarch64-pc-windows-msvc'} default {throw 'Unsupported Windows architecture.'} }
        $Download = Join-Path $ToolsDir ('download-' + [guid]::NewGuid().ToString())
        New-Item -ItemType Directory -Path $Download | Out-Null
        try {
            $Archive = "uv-$Target.zip"
            $Release = 'https://github.com/astral-sh/uv/releases/download/0.9.8'
            Invoke-WebRequest "$Release/$Archive" -OutFile (Join-Path $Download $Archive)
            Invoke-WebRequest "$Release/$Archive.sha256" -OutFile (Join-Path $Download 'checksum')
            $Expected = ((Get-Content (Join-Path $Download 'checksum') -Raw).Trim() -split '\s+')[0]
            $Actual = (Get-FileHash (Join-Path $Download $Archive) -Algorithm SHA256).Hash
            if ($Actual.ToLower() -ne $Expected.ToLower()) { throw 'uv checksum verification failed.' }
            Expand-Archive (Join-Path $Download $Archive) -DestinationPath $Download
            $Binary = Get-ChildItem $Download -Recurse -Filter uv.exe | Select-Object -First 1
            if (-not $Binary) { throw 'uv executable missing in archive.' }
            Copy-Item $Binary.FullName $Uv
        } finally { Remove-Item $Download -Recurse -Force }
    }
}
$Python = Join-Path $RuntimeDir '.venv/Scripts/python.exe'
if (-not (Test-Path $Python)) {
    & $Uv venv --managed-python --python 3.12 (Join-Path $RuntimeDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python provisioning failed.' }
}
$env:UV_PROJECT_ENVIRONMENT = Join-Path $RuntimeDir '.venv'
$SyncArgs = @('sync', '--project', (Join-Path $PackageDir 'runtime'), '--frozen', '--no-dev', '--no-editable', '--inexact')
if ($Embeddings) { $SyncArgs += @('--extra', 'embeddings') }
if ($Postgres) { $SyncArgs += @('--extra', 'postgres') }
& $Uv @SyncArgs
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency installation failed.' }
Write-Host 'Snytro runtime ready. Run scripts/snytro.ps1 doctor.'
