param(
    [int]$Port = 8765,
    [string]$Workspace = 'data/workbench'
)
$ErrorActionPreference = 'Stop'
$checkout = Split-Path -Parent $PSScriptRoot
Push-Location $checkout
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'DEPENDENCIES_MISSING: 请先安装 uv，再执行 uv sync --extra dev --extra web --extra data'
    }
    & uv sync --locked --check --extra dev --extra web --extra data
    if ($LASTEXITCODE -ne 0) { throw 'DEPENDENCIES_MISMATCH: 请执行 uv sync --locked --extra dev --extra web --extra data' }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'DEPENDENCIES_MISSING: 请安装 Node.js，再执行 npm.cmd --prefix web ci' }
    & npm.cmd --prefix web ls --depth=0
    if ($LASTEXITCODE -ne 0) { throw 'DEPENDENCIES_MISMATCH: 请执行 npm.cmd --prefix web ci' }
    & .venv/Scripts/python.exe -m quanto.cli serve --host 127.0.0.1 --port $Port --workspace $Workspace
    exit $LASTEXITCODE
} finally { Pop-Location }
