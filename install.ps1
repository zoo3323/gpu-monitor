# GPU Monitor agent installer for Windows (no WSL needed).
#
#   iex (irm 'http://HUB:PORT/install.ps1?t=TOKEN')
#       The line the hub prints. Installs the agent for the current user and starts it.
#       To pick the server's name:   $env:GPU_MONITOR_LABEL = 'NAME'; iex (irm '...')
#
#   powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\gpu-monitor\install.ps1" -Uninstall
#       Stop and remove it. Files are kept.
#
# Runs as the current user (no admin needed). Task Scheduler starts the agent in the
# background when you log in and checks it every 5 minutes. Needs Python 3.7+.
param(
  [string]$Hub = $env:GPU_MONITOR_HUB,
  [string]$Token = $env:GPU_MONITOR_TOKEN,
  [string]$Label = $env:GPU_MONITOR_LABEL,
  [string]$Lang = $env:GPU_MONITOR_LANG,
  [switch]$NoAi,
  [switch]$Uninstall
)

# hub 가 이 스크립트를 내줄 때 아래 세 줄을 채운다.
$ServedHub = ''
$ServedToken = ''
$ServedLang = ''

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Name = if ($env:GPU_MONITOR_NAME) { $env:GPU_MONITOR_NAME } else { 'gpu-monitor' }
$Dir = if ($env:GPU_MONITOR_DIR) { $env:GPU_MONITOR_DIR } else { Join-Path $env:LOCALAPPDATA $Name }
$EnvFile = Join-Path $Dir 'monitor.env'
$Task = "$Name-agent"
if ($env:GPU_MONITOR_NOAI -eq '1') { $NoAi = $true }
if ($env:GPU_MONITOR_UNINSTALL -eq '1') { $Uninstall = $true }

function Get-EnvValue([string]$key) {
  if (-not (Test-Path $EnvFile)) { return '' }
  $line = Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -like "$key=*" } | Select-Object -Last 1
  if (-not $line) { return '' }
  return $line.Substring($key.Length + 1).Trim().Trim("'").Trim('"')
}

# 화면 언어: -Lang > hub 가 채운 값 > 기존 monitor.env > en
$L = @($Lang, $ServedLang, (Get-EnvValue 'MONITOR_LANG'), 'en') | Where-Object { $_ -in @('en', 'ko') } | Select-Object -First 1
function T([string]$en, [string]$ko) { if ($script:L -eq 'ko') { $ko } else { $en } }
function Say([string]$m) { Write-Host $m }
function Warn([string]$m) { Write-Host "! $m" -ForegroundColor Yellow }
# 멈출 때는 exit 대신 예외를 쓴다 — iex 로 돌 때 exit 하면 사용자의 PowerShell 창이 닫힌다.
function Die([string]$m) { throw "GPUMON: $m" }

# hub 는 사내망·VPN 안에 있다. 시스템 프록시를 거치지 않고 바로 묻는다.
function Get-Url([string]$url, [int]$timeoutSec = 8) {
  $req = [System.Net.WebRequest]::Create($url)
  $req.Proxy = $null
  $req.Timeout = $timeoutSec * 1000
  $resp = $req.GetResponse()
  try {
    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
    return $reader.ReadToEnd()
  } finally { $resp.Close() }
}
function Save-Text([string]$path, [string]$text) {
  # BOM 을 붙인다 — 윈도우 PowerShell 5.1 은 BOM 없는 파일의 한글을 깨뜨린다.
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $true))
}
function Set-EnvValue([string]$key, [string]$value) {
  $lines = @()
  if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile -Encoding UTF8) }
  $quoted = if ($value -match "^[A-Za-z0-9_.,:/@+=-]*$") { $value } else { "'" + $value.Replace("'", "'\''") + "'" }
  $new = "$key=$quoted"
  $hit = $false
  for ($i = $lines.Count - 1; $i -ge 0; $i--) { if ($lines[$i] -like "$key=*") { $lines[$i] = $new; $hit = $true; break } }
  if (-not $hit) { $lines += $new }
  [System.IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
}

function Find-Python {
  $tries = @()
  if (Get-Command py -ErrorAction SilentlyContinue) { $tries += , @('py', '-3') }
  foreach ($n in @('python', 'python3')) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    # WindowsApps 의 python.exe 는 스토어를 여는 자리표시자다.
    if ($c -and $c.Source -notlike '*WindowsApps*') { $tries += , @($c.Source) }
  }
  foreach ($t in $tries) {
    try {
      $code = "import sys; print(sys.executable if sys.version_info >= (3, 7) else '')"
      $exe = if ($t.Count -gt 1) { & $t[0] $t[1] -c $code 2>$null } else { & $t[0] -c $code 2>$null }
      if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    } catch { }
  }
  return $null
}

function Stop-Agent {
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains((Join-Path $Dir 'agent.py')) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Remove-Agent {
  Say "$(T 'Uninstalling' '제거') ($Dir)"
  if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false
    Say "  $(T 'Scheduled task removed' '작업 스케줄러에서 지웠습니다'): $Task"
  }
  Stop-Agent
  Say "$(T 'Files are kept. To delete them:' '파일은 남겨 두었습니다. 지우려면:')  Remove-Item -Recurse '$Dir'"
}

function Install-Agent {
  if (-not $Hub) { $Hub = $ServedHub }
  if (-not $Hub) { $Hub = Get-EnvValue 'MONITOR_HUB' }
  if (-not $Token) { $Token = $ServedToken }
  if (-not $Token) { $Token = Get-EnvValue 'MONITOR_TOKEN' }
  if (-not $Hub) { Die (T "Hub address needed — run the line the hub printed" "hub 주소가 필요합니다 — hub가 알려 준 줄을 실행하세요") }
  Say "$(T 'Installing agent' '에이전트 설치') → $Dir"

  $py = Find-Python
  if (-not $py) {
    Die (T "Python 3.7+ is needed. Install it, open a new PowerShell and run this line again:  winget install -e --id Python.Python.3.12" `
           "Python 3.7 이상이 필요합니다. 설치한 뒤 새 PowerShell에서 이 줄을 다시 실행하세요:  winget install -e --id Python.Python.3.12")
  }
  $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
  if (-not (Test-Path $pyw)) { $pyw = $py }

  # hub 주소는 쉼표로 여러 개일 수 있다(사내망·VPN). 닿는 주소를 앞에 둔다.
  $reach = @(); $far = @()
  foreach ($u in ($Hub -split ',')) {
    $u = $u.Trim().TrimEnd('/')
    if (-not $u) { continue }
    try { Get-Url "$u/api/health" | Out-Null; $reach += $u } catch { $far += $u }
  }
  if ($reach.Count -eq 0) { Die "$(T "Can't reach the hub:" 'hub에 닿지 않습니다:') $Hub" }
  $first = $reach[0]
  $Hub = (@($reach) + @($far)) -join ','

  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  # install.ps1 도 받아 둔다 — 나중에 -Uninstall 로 내릴 수 있게.
  foreach ($f in @('agent.py', 'install.ps1')) { Save-Text (Join-Path $Dir $f) (Get-Url "$first/dist/$f" 30) }

  if (-not $Label) { $Label = Get-EnvValue 'MONITOR_LABEL' }
  if (-not $Label) { $Label = $env:COMPUTERNAME.ToLower() }
  # 같은 이름을 다른 서버가 이미 쓰면 화면에서 뒤섞인다 — 설정을 바꾸기 전에 멈춘다.
  try {
    $all = Get-Url "$first/api/all" 15 | ConvertFrom-Json
    $me = [System.Net.Dns]::GetHostName()
    $other = $all.machines | Where-Object { $_.machine_id -eq $Label -and $_.hostname -and $_.hostname -ne $me } | Select-Object -First 1
    if ($other) {
      Die (T "'$Label' is already used by another server ($($other.hostname)) — set `$env:GPU_MONITOR_LABEL = 'OTHER_NAME' and run again" `
             "'$Label' 이름을 다른 서버($($other.hostname))가 이미 씁니다 — `$env:GPU_MONITOR_LABEL = '다른이름' 을 먼저 하고 다시 실행하세요")
    }
  } catch { if ("$_" -like 'GPUMON:*') { throw } }

  if (-not (Test-Path $EnvFile)) {
    $head = if ($L -eq 'ko') { '# GPU 모니터 설정입니다. install.ps1이 만들었습니다. 항목 설명은 저장소의 monitor.env.example에 있습니다.' }
            else { '# GPU Monitor settings, written by install.ps1. Every option is described in monitor.env.example.' }
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]@($head), (New-Object System.Text.UTF8Encoding $false))
  }
  Set-EnvValue 'MONITOR_HUB' $Hub
  Set-EnvValue 'MONITOR_TOKEN' $Token
  Set-EnvValue 'MONITOR_LABEL' $Label
  Set-EnvValue 'MONITOR_LANG' $L
  if ($NoAi) { Set-EnvValue 'MONITOR_AI' 'off' }
  # 토큰이 들어 있다 — 이 사용자만 읽게 한다.
  icacls $EnvFile /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null

  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if ($smi) {
    $gpus = (& $smi.Source -L 2>$null) -replace ' \(UUID.*', ''
    Say "  GPU: $($gpus -join ', ')"
  } else {
    Warn (T "nvidia-smi not found — this PC will show CPU, memory and disk only" "nvidia-smi가 없습니다 — 이 PC는 CPU·메모리·디스크만 보입니다")
  }
  $ai = T 'AI identification' 'AI 식별'
  $claude = Get-Command claude -ErrorAction SilentlyContinue
  if ($NoAi -or (Get-EnvValue 'MONITOR_AI') -eq 'off') { Say "  ${ai}: $(T 'off' '꺼 둠') (MONITOR_AI=off)" }
  elseif (-not $claude) {
    Say "  ${ai}: $(T 'off — Claude Code is not installed. Install it and log in; it turns on within a minute:' '꺼짐 — Claude Code가 없습니다. 설치하고 로그인하면 1분 안에 자동으로 켜집니다:')"
    Say "      irm https://claude.ai/install.ps1 | iex"
    Say "      claude auth login"
  } elseif ((& $claude.Source auth status --json 2>$null | Out-String) -match '"loggedIn":\s*true') {
    Say "  ${ai}: $(T 'on' '켜짐') ($($claude.Source))"
  } else {
    Say "  ${ai}: $(T 'login needed — once on this PC:' '로그인이 필요합니다 — 이 PC에서 한 번:')  claude auth login"
  }

  # 작업 스케줄러: 로그인할 때, 그리고 5분마다 확인한다. 이미 돌고 있으면 새로 띄우지 않는다.
  Stop-Agent
  $user = "$env:USERDOMAIN\$env:USERNAME"
  $action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$(Join-Path $Dir 'agent.py')`"" -WorkingDirectory $Dir
  $every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
  $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  try {
    Register-ScheduledTask -TaskName $Task -Action $action -Trigger @($logon, $every) -Settings $settings -Principal $principal -Force | Out-Null
  } catch {
    # 관리자가 아니면 로그인 트리거를 거절하는 PC 가 있다 — 5분 주기만으로 등록한다.
    Register-ScheduledTask -TaskName $Task -Action $action -Trigger $every -Settings $settings -Principal $principal -Force | Out-Null
  }
  Start-ScheduledTask -TaskName $Task

  $ok = $false
  foreach ($i in 1..20) {
    Start-Sleep -Seconds 1
    try {
      $m = (Get-Url "$first/api/all" 10 | ConvertFrom-Json).machines | Where-Object { $_.machine_id -eq $Label -and $_.online }
      if ($m) { $ok = $true; break }
    } catch { }
  }
  if (-not $ok) { Die "$(T "The agent didn't show up on the hub — see" '에이전트가 hub에 나타나지 않았습니다 — 확인:') $(Join-Path $Dir 'agent.log')" }
  Say "  agent: $(T 'Task Scheduler' '작업 스케줄러') $Task ($(T 'log' '로그'): $(Join-Path $Dir 'agent.log'))"
  Say "  $(T 'Name' '이름'): $Label  →  $(T 'dashboard' '대시보드') $first"
}

try {
  if ($Uninstall) { Remove-Agent } else { Install-Agent }
} catch {
  $msg = "$_"
  if ($msg -like 'GPUMON:*') { Write-Host ("x " + $msg.Substring(8)) -ForegroundColor Red }
  else { Write-Host "x $msg" -ForegroundColor Red; Write-Host $_.ScriptStackTrace }
}
