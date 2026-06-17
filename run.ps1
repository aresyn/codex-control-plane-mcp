$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not $env:CODEX_CONTROL_PLANE_MCP_LOG -and -not $env:OPENCLAW_CODEX_MCP_LOG) {
    $env:CODEX_CONTROL_PLANE_MCP_LOG = Join-Path $logDir "server.log"
}
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$wrapperLog = Join-Path $logDir "wrapper.log"
$stderrLog = Join-Path $logDir ("stderr-{0}.log" -f $PID)

function Write-WrapperLog {
    param([string] $Message)
    $timestamp = (Get-Date).ToString("o")
    Add-Content -LiteralPath $wrapperLog -Encoding UTF8 -Value "$timestamp $Message"
}

try {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyCommand) {
        $pyCandidates = @(
            (Join-Path $env:SystemRoot "py.exe"),
            "C:\Windows\py.exe",
            "python.exe"
        )
        foreach ($candidate in $pyCandidates) {
            $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($resolved) {
                $pyCommand = $resolved
                break
            }
        }
    }
    if (-not $pyCommand) {
        throw "Python launcher not found: py.exe/python.exe is not available in PATH or C:\Windows"
    }
    $logPath = if ($env:CODEX_CONTROL_PLANE_MCP_LOG) { $env:CODEX_CONTROL_PLANE_MCP_LOG } else { $env:OPENCLAW_CODEX_MCP_LOG }
    Write-WrapperLog "starting codex-control-plane-mcp root=$PSScriptRoot py=$($pyCommand.Source) log=$logPath"
    $ErrorActionPreference = "Continue"
    & $pyCommand.Source -u -m codex_control_plane_mcp.server 2>> $stderrLog
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Write-WrapperLog "exited code=$exitCode"
    exit $exitCode
}
catch {
    Write-WrapperLog "failed $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
