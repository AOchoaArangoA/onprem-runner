<#
.SYNOPSIS
  Verifies an OPR Studio portable build the way a user on another PC would meet it.

.DESCRIPTION
  Downloads the release .exe (or uses -Exe), checks its SHA-256, runs it from a brand-new folder,
  and checks that the window opens, the engine answers, a shell-only pipeline runs and its logs reach
  the local database. Prints a PASS/FAIL line per check and the timings; leaves nothing running.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File verify-portable.ps1
  powershell -ExecutionPolicy Bypass -File verify-portable.ps1 -Exe C:\Downloads\OPR-Studio-0.1.1-portable.exe
#>
param(
  [string]$Version = '0.1.1',
  [string]$Exe,
  [string]$Sha256 = '',
  [int]$TimeoutSeconds = 180,
  [switch]$KeepFolder
)

$ErrorActionPreference = 'Stop'
$name = "OPR-Studio-$Version-portable.exe"
$url = "https://github.com/AOchoaArangoA/onprem-runner/releases/download/v$Version/$name"
$work = Join-Path ([IO.Path]::GetTempPath()) ("opr-verify-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$failed = 0

function Check($label, [bool]$ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $label $detail" -ForegroundColor Green }
  else { Write-Host "FAIL  $label $detail" -ForegroundColor Red; $script:failed++ }
}

function Stop-Opr {
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -match '^(OPR Studio|opr-engine|OPR-Studio-.*-portable)$' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force $work | Out-Null
Write-Host "Test folder: $work"

try {
  # 1. get the file
  $target = Join-Path $work $name
  if ($Exe) {
    Copy-Item $Exe $target
  } else {
    Write-Host "Downloading $url"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $target
    Write-Host ("Downloaded {0:N0} MB in {1:N0} s" -f ((Get-Item $target).Length / 1MB), $sw.Elapsed.TotalSeconds)
  }
  Check 'file present' (Test-Path $target) ("{0:N0} MB" -f ((Get-Item $target).Length / 1MB))

  $hash = (Get-FileHash $target -Algorithm SHA256).Hash.ToLower()
  Write-Host "SHA-256 $hash"
  if ($Sha256) { Check 'SHA-256 matches' ($hash -eq $Sha256.ToLower()) }

  # Mark of the Web: a browser download makes Windows show SmartScreen on first run
  $zone = Get-Content "$target`:Zone.Identifier" -ErrorAction SilentlyContinue
  if ($zone) { Write-Host 'NOTE  the file has the Mark of the Web: SmartScreen may ask "More info > Run anyway".' -ForegroundColor Yellow }

  # 2. start it
  Stop-Opr
  $clock = [Diagnostics.Stopwatch]::StartNew()
  Start-Process $target -WorkingDirectory $work | Out-Null

  $lock = Join-Path $work 'data\engine.lock'
  $engine = $null
  $windowAt = $null
  while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    Start-Sleep -Milliseconds 500
    if (-not $engine -and (Test-Path $lock)) {
      try { $engine = Get-Content $lock -Raw | ConvertFrom-Json } catch { }
    }
    if (-not $windowAt -and (Get-Process 'OPR Studio' -ErrorAction SilentlyContinue | Where-Object MainWindowTitle -eq 'OPR Studio')) {
      $windowAt = $clock.Elapsed.TotalSeconds
    }
    if ($engine -and $windowAt) { break }
  }
  Check 'engine started' ([bool]$engine) ("(port $($engine.port))")
  Check 'main window opened' ([bool]$windowAt) $(if ($windowAt) { "after {0:N1} s" -f $windowAt } else { '' })

  if ($engine) {
    $base = "http://127.0.0.1:$($engine.port)"
    $auth = @{ Authorization = "Bearer $($engine.token)" }
    $state = Invoke-RestMethod "$base/api/state" -Headers $auth
    Check 'engine API answers' ($null -ne $state)

    # 3. a pipeline that needs no Python: two steps, one after the other
    $pipelines = Join-Path $work 'pipelines'
    Set-Content -Encoding UTF8 (Join-Path $pipelines 'verify.yaml') @"
name: Verify portable
steps:
  - {id: one, type: shell, command: echo hello from opr}
  - {id: two, type: shell, command: echo second step, needs: [one]}
"@
    Start-Sleep -Seconds 2
    $run = Invoke-RestMethod "$base/api/pipeline/run?file=verify.yaml" -Method Post -Headers $auth -Body '{}' -ContentType 'application/json'
    $status = $null
    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 1
      $detail = Invoke-RestMethod "$base/api/runs/$($run.run_id)" -Headers $auth
      $status = $detail.status
      if ($status -notin 'running', 'queued') { break }
    }
    Check 'pipeline ran' ($status -eq 'success') "(status: $status)"
    $logs = Invoke-RestMethod "$base/api/runs/$($run.run_id)/logs" -Headers $auth
    Check 'logs stored in the local database' ((($logs | ConvertTo-Json -Depth 6) -match 'hello from opr'))
    Check 'database file created' (Test-Path (Join-Path $work 'data\*.duckdb'))
  }

  $log = Join-Path $work 'data\startup.log'
  if (Test-Path $log) { Write-Host "--- startup.log"; Get-Content $log }
}
finally {
  Stop-Opr
  Start-Sleep -Seconds 2
  if (-not $KeepFolder) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
  else { Write-Host "Kept: $work" }
}

if ($failed) { Write-Host "`n$failed check(s) failed." -ForegroundColor Red; exit 1 }
Write-Host "`nAll checks passed." -ForegroundColor Green
