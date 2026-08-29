@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未完成安装，请先双击 install_windows.cmd。
  pause
  exit /b 1
)
echo AI媒资库正在启动……
echo 浏览器访问：http://127.0.0.1:8000
echo 关闭本窗口即可停止服务。
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
