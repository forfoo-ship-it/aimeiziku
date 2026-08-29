$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 @Arguments
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python @Arguments
    }
    else {
        throw "未找到 Python。请先安装 Python 3.11 或更高版本，并勾选 Add Python to PATH。"
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败，退出代码：$LASTEXITCODE"
    }
}

Write-Host "正在检查 Python……" -ForegroundColor Cyan
Invoke-Python --version

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "正在创建独立运行环境……" -ForegroundColor Cyan
    Invoke-Python -m venv .venv
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Write-Host "正在安装项目依赖，首次安装可能需要几分钟……" -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip 更新失败，退出代码：$LASTEXITCODE"
}
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "依赖安装失败，退出代码：$LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "已创建本地 .env，请在其中填写视觉模型 API Key。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "AI媒资库安装完成。" -ForegroundColor Green
Write-Host "下一步：按需编辑 .env，然后双击 start_windows.cmd 启动服务。"
