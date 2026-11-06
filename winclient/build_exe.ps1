# 把客户端打成单个 exe（可选）
#
# 说明：exe 里只装"外壳"，不装 opencode-pool 内核。内核仍然由客户端用
# `python -m opencode_pool serve` 拉起 —— 因为内核要 `requests`、要能热改配置，
# 塞进 exe 只会让"改一行配置就要重新打包"。
#
# 用法：
#   pwsh -File build_exe.ps1
#   pwsh -File build_exe.ps1 -OneFile        # 单文件（启动稍慢）
#
# 产物：dist\OpenCodePoolClient\OpenCodePoolClient.exe

param(
    [switch]$OneFile,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

Write-Host "[1/4] 生成图标 …"
& $Python -c "import sys; sys.path.insert(0,'.'); from opencodepool_client import icon; print(icon.build_ico(r'app.ico'))"

Write-Host "[2/4] 安装/更新 PyInstaller …"
& $Python -m pip install --quiet --upgrade pyinstaller

Write-Host "[3/4] 打包 …"
$pyArgs = @(
    "--noconfirm", "--clean", "--windowed",
    "--name", "OpenCodePoolClient",
    "--icon", "app.ico",
    "--paths", ".",
    "--collect-submodules", "opencodepool_client"
)
if ($OneFile) { $pyArgs += "--onefile" }
$pyArgs += "run_client.py"
& $Python -m PyInstaller @args

Write-Host "[4/4] 完成"
if ($OneFile) {
    Write-Host "  产物：dist\OpenCodePoolClient.exe"
} else {
    Write-Host "  产物：dist\OpenCodePoolClient\OpenCodePoolClient.exe"
}
Write-Host ""
Write-Host "首次运行请在「设置」里确认："
Write-Host "  · 项目目录 = opencode-pool 仓库根（能看见 opencode_pool\ 那一层）"
Write-Host "  · Python    = 装了 requests 的解释器（留空则用打包时的解释器）"

