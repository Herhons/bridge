<#
.SYNOPSIS
    Optional external executor wrapper with quota-exhaustion fallback (Bridge v0.2.3).
    Runs OpenCode on a task card; if OpenCode exits with quota/rate-limit errors,
    automatically forwards the saved checkpoint + remaining task to WorkBuddy (free 0.00x tier).
.EXAMPLE
    .\scripts\run_with_fallback.ps1 -CardPath .\runs\card_v023.txt -CheckpointPath .\runs\v023_logs\checkpoint.json
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$CardPath,
    [string]$CheckpointPath = ".\runs\v023_logs\checkpoint.json",
    [string]$FallbackEndpoint = "workbuddy",
    [int]$TimeoutSec = 300
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $CardPath)) {
    Write-Error "Card file not found: $CardPath"
    exit 2
}

$logDir = Split-Path -Parent $CheckpointPath
if ($logDir -and -not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$cardText = Get-Content -Path $CardPath -Raw -Encoding UTF8
$outLog = Join-Path $logDir "opencode_runner.log"

Write-Host "[runner] Starting OpenCode with card: $CardPath"
$procOutput = & opencode --prompt $cardText --auto 2>&1 | Tee-Object -FilePath $outLog
$rc = $LASTEXITCODE
$joined = ($procOutput | Out-String)

$quotaRegex = "(?i)(quota|rate\s*limit|insufficient|limit\s*exceeded|too\s*many\s*requests|429|额度|余额不足|超出限制)"
if ($rc -eq 0 -and -not ($joined -match $quotaRegex)) {
    Write-Host "[runner] OpenCode finished cleanly (exit 0)."
    exit 0
}

if ($joined -match $quotaRegex) {
    Write-Warning "[runner] Detected quota/rate-limit exhaustion in OpenCode (exit $rc). Handing off to $FallbackEndpoint (--profile test)..."
    $ckpt = if (Test-Path $CheckpointPath) { Get-Content -Path $CheckpointPath -Raw -Encoding UTF8 } else { "{}" }
    $handoffFile = Join-Path $logDir "handoff_prompt.txt"
    $handoffPrompt = @"
【自动故障转移接力】主执行器 OpenCode 因额度耗尽中断（exit=$rc）。
请你从以下断点继续完成剩余步骤，已完成步骤不要重复执行：

=== Checkpoint ($CheckpointPath) ===
$ckpt

=== 原始任务卡 ===
$cardText
"@
    Set-Content -Path $handoffFile -Value $handoffPrompt -Encoding UTF8
    py -3.10 bridge.py ask $FallbackEndpoint --profile test --headed --timeout $TimeoutSec --file $handoffFile
    exit $LASTEXITCODE
}

Write-Warning "[runner] OpenCode exited with code $rc (non-quota error); not auto-failing over. Inspect $outLog"
exit $rc
