param([string]$Command, [string[]]$Next, [string]$Name = "two", [switch]$AsObject)
# Two instances of the same app ("Agent Sandbox A" and "B"), then a conversation: the command and each
# -Next utterance run through the tray app's path in one session (--cards). Prints every turn and both logs.
$S = $PSScriptRoot; $Repo = (Resolve-Path "$S\..\..").Path; $logs = @{}; $procs = @()
if (-not (Test-Path "$S\AgentSandbox.exe")) { & "$S\build-sandbox.ps1" | Out-Null }
foreach ($x in "A", "B") {
  $logs[$x] = "$env:TEMP\agent-sandbox-$Name-$x.log"; Remove-Item $logs[$x] -ErrorAction SilentlyContinue
  $procs += Start-Process "$S\AgentSandbox.exe" -ArgumentList "`"$($logs[$x])`" plain $x" -PassThru
}
try {
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline -and ($procs | Where-Object { -not (Get-Process -Id $_.Id -ErrorAction SilentlyContinue).MainWindowTitle })) { Start-Sleep -Milliseconds 300 }
  Push-Location $Repo
  $extra = @('--cards', '--confirm', 'yes'); foreach ($n in $Next) { $extra += '--answer', $n }
  $out = uv run --script bolt_dictate.py --agent $Command @extra 2>&1 | Out-String
  Pop-Location
  $i = $out.IndexOf("{`n"); if ($i -lt 0) { $i = $out.IndexOf("{`r`n") }
  $logText = @{}; foreach ($x in "A", "B") { $logText[$x] = if (Test-Path $logs[$x]) { (Get-Content $logs[$x]) -join ' / ' } else { '' } }
  if ($i -lt 0) {
    if ($AsObject) { return [pscustomobject]@{ Name = $Name; Turns = @(); LogA = $logText.A; LogB = $logText.B; Raw = $out.Trim() } }
    "  RAW: $out"; return
  }
  $r = $out.Substring($i) | ConvertFrom-Json
  if ($AsObject) { return [pscustomobject]@{ Name = $Name; Turns = $r.turns; LogA = $logText.A; LogB = $logText.B; Raw = '' } }
  "=== $Name"
  "  user: $Command"
  foreach ($t in $r.turns) { if ($t.answer) { "  user: $($t.answer)" }; "    -> $($t.status): $($t.say)"; "       steps: $($t.steps)" }
  "  gates (last turn): " + (($r.gates | % { "[$($_.allowed)] $($_.what -replace '\s+', ' ')" }) -join ' || ')
  foreach ($x in "A", "B") { "  sandbox $x log: " + $(if ($logText[$x]) { $logText[$x] } else { '<empty>' }) }
} finally {
  $procs | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
}
