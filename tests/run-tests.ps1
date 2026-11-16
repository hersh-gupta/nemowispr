param([switch]$SkipDictation, [switch]$SkipAgent)
# The whole suite with pass/fail counts. Needs Bolt on :8080 (scripts\run-bolt.ps1).
# The agent cases open sandbox windows and take focus for a few minutes: don't type meanwhile
# (a focus change makes the agent stop, which shows up here as a failure).
$T = $PSScriptRoot; $Repo = (Resolve-Path "$T\..").Path; $E = "$T\agent\agent-e2e.ps1"; $E2 = "$T\agent\agent-e2e-two.ps1"
$results = [System.Collections.Generic.List[object]]::new()
function Check($name, [bool]$ok, $detail) {
  $results.Add([pscustomobject]@{ Test = $name; Pass = $ok; Detail = $detail })
  Write-Host ("{0} {1}  {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $name, $detail) -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}

try { Invoke-RestMethod http://127.0.0.1:8080/health -TimeoutSec 3 | Out-Null } catch { throw "Bolt isn't running on :8080 -- start scripts\run-bolt.ps1" }

Push-Location $Repo
try {
  $smoke = uv run --script nemowispr.py --smoke 2>&1 | Out-String
  Check "startup smoke" ($LASTEXITCODE -eq 0) (($smoke -split "`n" | Where-Object { $_ -match 'FAIL' }) -join '; ')
  if (-not $SkipDictation) {
    $st = uv run --script nemowispr.py --selftest 2>&1 | Out-String
    $m = [regex]::Match($st, '(\d+)/(\d+) passed')
    # 9/10 is the known baseline: the email clip's trailing "Thanks." gets dropped
    Check "dictation selftest" ($m.Success -and [int]$m.Groups[1].Value -ge 9) $m.Value
  }
} finally { Pop-Location }

if (-not $SkipAgent) {
  $send = "In the Agent Sandbox window, type hello from Bolt in the message box, tick urgent, and press send"
  $sent = "SENT: hello from Bolt | urgent=True"
  $r = & $E -Command $send -Confirm yes -Cards -Name t-yes -AsObject
  Check "send, confirmed" ($r.Status -eq 'done' -and $r.Log -eq $sent) "$($r.Status) | $($r.Log)"
  $r = & $E -Command $send -Confirm no -Cards -Name t-no -AsObject
  Check "send, declined" ($r.Status -eq 'aborted' -and -not $r.Log) "$($r.Status) | $($r.Say)"
  $r = & $E -Command $send -Confirm yes -Cards -Mangle -Name t-mangle -AsObject
  Check "garbled text is caught, nothing sent" ($r.Status -eq 'failed' -and -not $r.Log) "$($r.Status) | $($r.Say)"
  $r = & $E -Command $send -Confirm yes -Cards -Busy -Name t-busy -AsObject
  Check "busy window (100 decoys first)" ($r.Status -eq 'done' -and $r.Log -eq $sent) "$($r.Status) | $($r.Log)"
  $r = & $E -Command "In the purple zebra window, type hello from Bolt in the message box, tick urgent and press send" `
            -Answer "Sorry, I meant the Agent Sandbox window" -Confirm yes -Cards -Name t-answer -AsObject
  Check "unknown window -> question/answer" ($r.Status -eq 'done' -and $r.Log -eq $sent) "$($r.Status) | $($r.Log)"
  $r = & $E2 -Command "In Agent Sandbox B, type hello from Bolt in the message box" `
             -Next "now tick urgent in that same window and press send", "which window did you just use?", "start over" -Name t-two -AsObject
  $t = @($r.Turns)
  Check "two windows: follow-up stays in B" ($r.LogB -eq $sent -and -not $r.LogA) "A='$($r.LogA)' B='$($r.LogB)'"
  Check "two windows: answers from memory" ($t.Count -ge 3 -and $t[2].status -eq 'done' -and $t[2].say -match '\bB\b') "$($t[2].say)"
  Check "two windows: 'start over' resets" ($t.Count -ge 4 -and $t[3].say -eq 'Okay, starting over.') "$($t[3].say)"
  $r = & $E2 -Command "In Agent Sandbox B, type hello from Bolt in the message box" -Next "now close that window without saving" -Name t-close -AsObject
  Check "close without saving (in-window prompt)" ($r.LogB -eq "DISCARDED: hello from Bolt" -and -not $r.LogA) "A='$($r.LogA)' B='$($r.LogB)'"
}

$pass = @($results | Where-Object Pass).Count
Write-Host ""
Write-Host "$pass/$($results.Count) passed" -ForegroundColor $(if ($pass -eq $results.Count) { "Green" } else { "Yellow" })
