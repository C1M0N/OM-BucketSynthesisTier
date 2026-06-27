# run_workflow.ps1 — osu!mania 4K 复合难度分级 一键工作流（Windows 本地版）
# 自定位：脚本所在 = <osu根>/4k_classification/engine。供手动运行与 Windows 计划任务调用。
# 只读游戏文件；osu!lazer 开着 / realm 半截时优雅跳过（exit 7 = REALM_BUSY）。
[CmdletBinding()]
param(
  [switch]$ReportOnly,        # 只重出报告，不重算/重建 db
  [switch]$Force,             # 强制重建 collection.db（删除现有 engine/collection.db）
  [switch]$ResetBaseline,     # 删除 .baseline_4k.json，让 newMaps 反映“本次真正新增”
  [switch]$Auto,              # 定时任务调用时加此开关：归档文件名前缀用 a（手动则为 m）
  [string[]]$PassArgs = @()   # 透传给 run_4k.py 的参数（如 --max-new=50）
)
$ErrorActionPreference = "Stop"

$HERE    = Split-Path -Parent $MyInvocation.MyCommand.Path     # .../4k_classification/engine
$DELIVER = Split-Path -Parent $HERE                            # .../4k_classification
$OSU_DIR = Split-Path -Parent $DELIVER                         # .../12_osu
$REALM   = Join-Path $OSU_DIR "client.realm"
$LOGDIR  = Join-Path $env:LOCALAPPDATA "osu4k\logs"   # 非 Dropbox：避免同步进程锁住日志文件
New-Item -ItemType Directory -Force $LOGDIR | Out-Null
$LOG     = Join-Path $LOGDIR ("run_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")

function Log($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
  Write-Host $line
  for ($i = 0; $i -lt 6; $i++) {
    try { Add-Content -Path $LOG -Value $line -Encoding UTF8 -ErrorAction Stop; break }
    catch { Start-Sleep -Milliseconds 120 }   # 偶发文件锁（杀软/同步）-> 重试
  }
}

Log "engine = $HERE"
Log "osu    = $OSU_DIR"

# ---- 工具定位（绝对路径，计划任务环境 PATH 不可靠）----
$PY = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $PY) { Log "ERROR: 找不到 Python（$env:LOCALAPPDATA\Programs\Python\Python3*）"; exit 1 }
$MIKTEXBIN = "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64"
$NODEDIR = if (Test-Path "C:\Program Files\nodejs\node.exe") { "C:\Program Files\nodejs" } else { (Split-Path (Get-Command node -ErrorAction SilentlyContinue).Source) }
$env:PATH = "$NODEDIR;$MIKTEXBIN;$env:PATH"
Log "python = $PY"
Log "node   = $NODEDIR ;  miktex = $MIKTEXBIN"

# ---- 计算器环境（持久，真实 FS；缺失则从 tar 自解包）----
$CALC = Join-Path $env:USERPROFILE "osu4k_calc"
$env:CALC_REPO = Join-Path $CALC "ManiaMapAnalyser by Leo_Black"
if (-not (Test-Path (Join-Path $env:CALC_REPO "batch_4k.mjs"))) {
  Log "calc env 缺失 -> 从 calc_env.tar.gz 解包到 $CALC"
  New-Item -ItemType Directory -Force $CALC | Out-Null
  tar.exe -xzf (Join-Path $HERE "calc_env.tar.gz") -C $CALC
  Copy-Item (Join-Path $HERE "node_runner\*.mjs") $env:CALC_REPO -Force
}
Log "calc   = $($env:CALC_REPO)"

# ---- osu! 是否在跑 ----
if (Get-Process -Name "osu!" -ErrorAction SilentlyContinue) {
  Log "REALM_BUSY: osu!lazer 正在运行 —— 本次跳过、未改动任何文件。请关闭 lazer 后再跑。"
  exit 7
}

# ---- realm 健康检查：top_ref 必须落在 [0, filesize) ----
function Test-RealmHealthy([string]$path) {
  try {
    $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
      $len = $fs.Length
      $hdr = New-Object byte[] 24; [void]$fs.Read($hdr, 0, 24)
      $sel = $hdr[22] -band 1
      [void]$fs.Seek($(if ($sel) {8} else {0}), 'Begin')
      $b8 = New-Object byte[] 8; [void]$fs.Read($b8, 0, 8)
      $top = [System.BitConverter]::ToUInt64($b8, 0)
      return ($top -gt 0 -and $top -lt [uint64]$len)
    } finally { $fs.Close() }
  } catch { return $false }
}
if (-not (Test-Path $REALM)) { Log "ERROR: 找不到 client.realm: $REALM"; exit 1 }
if (-not (Test-RealmHealthy $REALM)) {
  Log "REALM_BUSY: client.realm 半截/锁定（osu!lazer 开着或同步未完）—— 本次跳过、未改动任何文件。"
  exit 7
}
Log "realm 健康检查通过"

# ---- 可选：重置 baseline / 强制重建 db ----
if ($ResetBaseline) { $b = Join-Path $HERE ".baseline_4k.json"; if (Test-Path $b) { Remove-Item $b -Force; Log "已删除 .baseline_4k.json（newMaps 将反映本次真正新增）" } }
if ($Force)         { $d = Join-Path $HERE "collection.db";     if (Test-Path $d) { Remove-Item $d -Force; Log "已删除 engine/collection.db（强制重建）" } }

# ---- 主流程 ----
if (-not $ReportOnly) {
  Log "运行 run_4k.py $($PassArgs -join ' ')"
  & $PY (Join-Path $HERE "run_4k.py") @PassArgs 2>&1 | ForEach-Object { Write-Host $_; Add-Content -Path $LOG -Value $_ -Encoding UTF8 -ErrorAction SilentlyContinue }
  if ($LASTEXITCODE -ne 0) { Log "run_4k.py 失败 (exit $LASTEXITCODE)"; exit 1 }
}
Log "运行 make_report.py"
& $PY (Join-Path $HERE "make_report.py") 2>&1 | ForEach-Object { Write-Host $_; Add-Content -Path $LOG -Value $_ -Encoding UTF8 -ErrorAction SilentlyContinue }
if ($LASTEXITCODE -ne 0) { Log "make_report.py 失败 (exit $LASTEXITCODE)"; exit 1 }

# ---- 交付：拷到 4k_classification/ ----
$rep = Join-Path $HERE "report.pdf"
$db  = Join-Path $HERE "collection.db"
if (Test-Path $rep) { Copy-Item $rep (Join-Path $DELIVER "4k_report.pdf") -Force; Log "-> 4k_report.pdf" }
# 增量归档：report\4k-{m|a}YYMMDDHHMM.pdf （m=手动, a=定时自动；每次保留一份，不覆盖）
if (Test-Path $rep) {
  $REPDIR = Join-Path $DELIVER "report"
  New-Item -ItemType Directory -Force $REPDIR | Out-Null
  $tag = if ($Auto) { "a" } else { "m" }
  $arch = Join-Path $REPDIR ("4k-" + $tag + (Get-Date -Format "yyMMddHHmm") + ".pdf")
  Copy-Item $rep $arch -Force
  Log ("-> archive: report\" + (Split-Path $arch -Leaf))
}
if (Test-Path $db)  { Copy-Item $db  (Join-Path $DELIVER "4k_collections.db") -Force; Log "-> 4k_collections.db" }
Log "DONE. 日志: $LOG"
