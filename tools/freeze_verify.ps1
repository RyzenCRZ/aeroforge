# =====================================================================
# AeroForge 交付链复验一键脚本（规格 §16.2「交付链复验的固定步骤」的固化）
# =====================================================================
# 用途：M7 重冻后由主线一键跑完全部可自动化的复验步骤，缺一步即视为未完成。
# 用法（在仓库根执行）：
#     powershell -ExecutionPolicy Bypass -File tools\freeze_verify.ps1
#     可选开关：-SkipBuild / -SkipHashCheck / -SkipPreflight / -SkipProbe / -SkipGates
#     （-SkipProbe 同时跳过第 5 步残留比对——两者共用一次探针运行）
#
# 步骤 ↔ §16.2 固定步骤表对应：
#   1 重冻（产物必须落 dist-desktop/，不得漂移到默认 dist/——附录 D 冻结命令含 --distpath）
#   2 核对体积、文件数与包内前端哈希（"交付物真的换新了"的唯一判据）
#   3 冻结态 --preflight（三项全通过、exit = 0）
#   4 冻结态 --probe（WebGL2 可用、三基线打印；需交互桌面会话）
#   5 退出零残留（探针前后 msedgewebview2 按 (Id, 启动时间) 逐条比对 + AeroForge 无进程）
#   6 断网复跑（人工/沙箱动作，本脚本只输出方法学与命令，见下方说明块）
#   7 全量门禁复跑（后端 pytest / ruff check / ruff format --check / mypy；前端 typecheck / test / build）
#
# ========================= 第 6 步方法学（脚本不代跑） =========================
# 断网复跑须在「AI 助手未配置」状态下执行（§10.2），判据：--preflight / --probe 通过 +
# 完整走一遍「改参 → 预览 → 构建」（§13.7 步骤 7）。
#
# 模拟手段与各自的局限（**真实断网（拔网线/关 Wi-Fi/禁网卡）仍是唯一权威口径**，建议人工执行）：
#   a) 代理黑洞：把 HTTP(S)_PROXY / ALL_PROXY 指向 127.0.0.1:9 再跑探针——
#        $env:HTTP_PROXY='http://127.0.0.1:9'; $env:HTTPS_PROXY='http://127.0.0.1:9'
#      局限：只拦"遵守代理环境变量的"请求；直连 IP、WinHTTP/WinINET 层请求、WebView2
#      自身的系统代理解析都可能绕过它，覆盖不完整。
#   b) hosts 局锁：把可疑域名追加到 C:\Windows\System32\drivers\etc\hosts 的 0.0.0.0——
#      局限：必须先**穷举**域名（漏一个就假绿），且对直连 IP 无效；DNS over HTTPS 不受 hosts 约束。
#   c) Windows 防火墙出站规则：对 AeroForge.exe / msedgewebview2.exe 建立阻止出站规则——
#        New-NetFirewallRule -DisplayName "AeroForge offline-test" -Direction Outbound -Program "<path>" -Action Block
#      局限：按进程路径匹配，子进程（WebView2 子进程与主 exe 同目录，可覆盖）之外的
#      系统级联网（如 Windows 自身）不受影响——对本应用足够，但不是"物理断网"。
#   三种手段都通过后仍应以真实断网复跑一次作为最终判据（OI-15 的本意是守护 CON-01 红线，
#   模拟手段的价值在于日常高频自检，而非替代终审）。
# ==============================================================================

param(
    [switch]$SkipBuild,
    [switch]$SkipHashCheck,
    [switch]$SkipPreflight,
    [switch]$SkipProbe,   # 第 4、5 步共用探针，跳过探针即同时跳过残留比对
    [switch]$SkipGates
)

$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
$Exe  = Join-Path $Repo "dist-desktop\AeroForge\AeroForge.exe"
$Uv   = Join-Path $Repo ".tools\uv\uv.exe"
$Failures = New-Object System.Collections.Generic.List[string]

# uv 工程内工具链口径（§15：不写系统路径）
$env:UV_CACHE_DIR          = Join-Path $Repo ".tools\uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Repo ".tools\python"

function Write-Step([string]$msg) { Write-Host "`n======== $msg ========" -ForegroundColor Cyan }
function Write-Pass([string]$msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Write-Fail([string]$msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; $script:Failures.Add($msg) }
function Write-Note([string]$msg) { Write-Host "  [提示] $msg" -ForegroundColor Yellow }

if (-not (Test-Path $Exe) -and ($SkipBuild)) {
    Write-Fail "dist-desktop\AeroForge\AeroForge.exe 不存在，且已跳过构建——无产物可验"
}

# ---------- 第 1 步：重冻 ----------
if (-not $SkipBuild) {
    Write-Step "第 1 步：重冻（PyInstaller onedir → dist-desktop\）"
    Push-Location $Repo
    & $Uv run pyinstaller desktop/packaging/aeroforge.spec --distpath dist-desktop --workpath .tools/pyinstaller-build --noconfirm
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -eq 0 -and (Test-Path $Exe)) { Write-Pass "冻结完成，产物位于 dist-desktop\AeroForge\（未漂移到默认 dist\）" }
    else { Write-Fail "冻结失败（exit=$code）或产物未落在 dist-desktop\（§16.2 曾发生过的漂移）" }
}

# ---------- 第 2 步：体积 / 文件数 / 包内前端哈希 ----------
if (-not $SkipHashCheck) {
    Write-Step "第 2 步：核对体积、文件数与包内前端哈希"
    $bundle = Join-Path $Repo "dist-desktop\AeroForge"
    $files = Get-ChildItem $bundle -Recurse -File
    $sizeMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "  产物体积：$sizeMB MB；文件数：$($files.Count)"
    if ($sizeMB -gt 550) { Write-Fail "体积 $sizeMB MB 超出 §13.5.1 预算 550 MB" }
    else { Write-Pass "体积 $sizeMB MB 在预算内（≤550 MB）" }

    # 包内前端与本次 npm run build 逐字节一致（"换新了"的唯一判据）
    $srcRoot = Join-Path $Repo "frontend\dist"
    $dstRoot = Join-Path $bundle "_internal\frontend\dist"
    $bad = 0; $missing = 0; $checked = 0
    Get-ChildItem $srcRoot -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart('\')
        $inBundle = Join-Path $dstRoot $rel
        $checked++
        if (-not (Test-Path $inBundle)) { $missing++; Write-Host "    包内缺失：$rel" }
        elseif ((Get-FileHash $_.FullName).Hash -ne (Get-FileHash $inBundle).Hash) { $bad++; Write-Host "    哈希不一致：$rel" }
    }
    if ($checked -eq 0) { Write-Fail "frontend\dist 为空——请先 npm run build" }
    elseif ($bad -eq 0 -and $missing -eq 0) { Write-Pass "包内前端 $checked 个文件与本次构建逐字节一致" }
    else { Write-Fail "包内前端与本次构建不一致（缺失 $missing / 哈希不符 $bad）——交付物不是最新的" }
}

# ---------- 第 3 步：冻结态 --preflight ----------
if (-not $SkipPreflight) {
    Write-Step "第 3 步：冻结态 --preflight（字体 / OCCT / CEA 三项）"
    & $Exe --preflight
    if ($LASTEXITCODE -eq 0) { Write-Pass "预检三项全通过（exit=0）" }
    else { Write-Fail "--preflight exit=$($LASTEXITCODE)（判据：0）" }
}

# ---------- 第 4、5 步：冻结态 --probe + 退出零残留 ----------
if (-not $SkipProbe) {
    Write-Step "第 4/5 步：冻结态 --probe + 退出零残留"
    Write-Note "探针会打开真实窗口，须在交互桌面会话内执行（远程/无头环境会失败）"
    # 残留基线：只记 (Id, StartTime)——只比数量会把别家应用的 WebView2 误判为孤儿（§16.2）
    $before = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Select-Object Id, StartTime)
    & $Exe --probe
    $probeCode = $LASTEXITCODE
    if ($probeCode -eq 0) { Write-Pass "--probe exit=0（基线数字见上方启动报告）" }
    else { Write-Fail "--probe exit=$probeCode（判据：0）" }

    Start-Sleep -Seconds 2
    $after = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Select-Object Id, StartTime)
    $orphans = @($after | Where-Object { $o = $_; -not ($before | Where-Object { $_.Id -eq $o.Id -and $_.StartTime -eq $o.StartTime }) })
    if ($orphans.Count -eq 0) { Write-Pass "msedgewebview2 无新增残留（探针前后按 Id+启动时间比对）" }
    else { Write-Fail "发现 $($orphans.Count) 个疑似孤儿 WebView2 进程：$($orphans | ForEach-Object Id)" }

    $af = Get-Process AeroForge -ErrorAction SilentlyContinue
    if ($null -eq $af) { Write-Pass "AeroForge 无残留进程" }
    else { Write-Fail "AeroForge 仍有进程：PID $($af.Id)" }
}

# ---------- 第 6 步：断网复跑（人工，脚本只给方法学） ----------
Write-Step "第 6 步：断网复跑（人工 / 沙箱动作，本脚本不代跑）"
Write-Note "方法学见本脚本头部注释块（代理黑洞 / hosts 局锁 / 防火墙出站规则及其局限）。"
Write-Note "建议命令序列："
Write-Host '    $env:HTTP_PROXY="http://127.0.0.1:9"; $env:HTTPS_PROXY="http://127.0.0.1:9"'
Write-Host '    dist-desktop\AeroForge\AeroForge.exe --preflight && dist-desktop\AeroForge\AeroForge.exe --probe'
Write-Note "须在 AI 助手未配置状态下跑，并完整走一遍「改参 → 预览 → 构建」（§13.7 步骤 7）。真实断网（拔线/关网卡）为最终判据。"

# ---------- 第 7 步：全量门禁 ----------
if (-not $SkipGates) {
    Write-Step "第 7 步：全量门禁复跑"
    Push-Location $Repo
    & $Uv run pytest -q;                    if ($LASTEXITCODE -eq 0) { Write-Pass "pytest" } else { Write-Fail "pytest exit=$LASTEXITCODE" }
    & $Uv run ruff check .;                 if ($LASTEXITCODE -eq 0) { Write-Pass "ruff check" } else { Write-Fail "ruff check exit=$LASTEXITCODE" }
    & $Uv run ruff format --check .;        if ($LASTEXITCODE -eq 0) { Write-Pass "ruff format --check" } else { Write-Fail "ruff format --check exit=$LASTEXITCODE" }
    & $Uv run mypy backend;                 if ($LASTEXITCODE -eq 0) { Write-Pass "mypy" } else { Write-Fail "mypy exit=$LASTEXITCODE" }
    Pop-Location
    Push-Location (Join-Path $Repo "frontend")
    npm run typecheck;                      if ($LASTEXITCODE -eq 0) { Write-Pass "npm run typecheck" } else { Write-Fail "typecheck exit=$LASTEXITCODE" }
    npm run test;                           if ($LASTEXITCODE -eq 0) { Write-Pass "npm run test" } else { Write-Fail "test exit=$LASTEXITCODE" }
    npm run build;                          if ($LASTEXITCODE -eq 0) { Write-Pass "npm run build" } else { Write-Fail "build exit=$LASTEXITCODE" }
    Pop-Location
}

# ---------- 汇总 ----------
Write-Step "复验汇总"
if ($Failures.Count -eq 0) {
    Write-Host "全部自动化步骤通过。剩余人工项：第 6 步断网复跑（见上方方法学）。" -ForegroundColor Green
    exit 0
} else {
    Write-Host "失败 $($Failures.Count) 项：" -ForegroundColor Red
    $Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
