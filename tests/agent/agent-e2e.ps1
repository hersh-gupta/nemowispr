param([string]$Command, [ValidateSet('yes', 'no')][string]$Confirm, [string]$Name, [switch]$DryRun,
      [string[]]$Answer, [switch]$Speak, [switch]$Mangle, [switch]$Cards, [switch]$Busy, [string]$Prefill,
      [switch]$AsObject)
# One end-to-end agent run against a fresh Agent Sandbox window; prints the sandbox log + agent result.
# Omit -Confirm to get the REAL confirmation dialog and answer it yourself. -AsObject returns the parsed
# result plus the sandbox log (for run-agent-tests.ps1) instead of printing.
$S = $PSScriptRoot; $Repo = (Resolve-Path "$S\..\..").Path; $Log = "$env:TEMP\agent-sandbox-$Name.log"
if (-not (Test-Path "$S\AgentSandbox.exe")) { & "$S\build-sandbox.ps1" | Out-Null }
Remove-Item $Log -ErrorAction SilentlyContinue
$mode = if ($Mangle) { 'mangle' } elseif ($Busy) { 'busy' } else { 'plain' }
$sbArgs = "`"$Log`" $mode -"; if ($Prefill) { $sbArgs += " `"$Prefill`"" }
$sb = Start-Process "$S\AgentSandbox.exe" -ArgumentList $sbArgs -PassThru
try {
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline -and -not (Get-Process -Id $sb.Id -ErrorAction SilentlyContinue).MainWindowTitle) { Start-Sleep -Milliseconds 300 }
  Push-Location $Repo
  $extra = @(); if ($DryRun) { $extra += '--dry-run' }; if ($Confirm) { $extra += '--confirm', $Confirm }
  foreach ($a in $Answer) { $extra += '--answer', $a }; if ($Speak) { $extra += '--speak' }; if ($Cards) { $extra += '--cards' }
  $out = uv run --script nemowispr.py --agent $Command @extra 2>&1 | Out-String
  Pop-Location
  $i = $out.IndexOf("{`n"); if ($i -lt 0) { $i = $out.IndexOf("{`r`n") }
  $sandboxLog = if (Test-Path $Log) { (Get-Content $Log) -join ' / ' } else { '' }
  if ($i -lt 0) {
    if ($AsObject) { return [pscustomobject]@{ Name = $Name; Status = 'error'; Say = $out.Trim(); Log = $sandboxLog; Result = $null } }
    "  RAW: $out"; return
  }
  $r = $out.Substring($i) | ConvertFrom-Json
  if ($AsObject) { return [pscustomobject]@{ Name = $Name; Status = $r.status; Say = $r.say; Log = $sandboxLog; Result = $r } }
  if ($i -gt 0) { "  (pre-JSON output: $($out.Substring(0, [math]::Min($i, 300)).Trim()))" }
  "=== $Name (confirm=$Confirm): status=$($r.status) verified=$($r.verified) steps=$($r.steps) $($r.secs)s"
  "  summary: $($r.summary)"
  "  spoken:  $($r.say)"
  if ($r.turns.Count -gt 1) { "  turns:   " + (($r.turns | % { "$(if ($_.answer) { "<user: $($_.answer)> " })$($_.status): $($_.say)" }) -join ' || ') }
  if ($r.problems -or $r.unconfirmed) { "  checks:  problems=$($r.problems -join '; ') unconfirmed=$($r.unconfirmed -join ', ')" }
  "  gates:   " + (($r.gates | % { "[$($_.allowed)] $($_.what -replace '\s+', ' ')" }) -join ' || ')
  "  steps:   " + (($r.trace | % { "$($_.tool)($((($_.args | ConvertTo-Json -Compress) -replace '^\{\}$', '')))" }) -join ' -> ')
  "  sandbox log: " + $(if ($sandboxLog) { $sandboxLog } else { '<empty>' })
} finally {
  Stop-Process -Id $sb.Id -Force -ErrorAction SilentlyContinue
}
