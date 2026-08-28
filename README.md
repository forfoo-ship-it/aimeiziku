# 县媒智搜——县级融媒体声画智能检索系统

面向县级融媒体中心编辑的历史素材检索系统。本仓库当前只完成第一阶段：上传 MP4 视频、每隔 5 秒提取关键帧、保存准确时间点，并在网页中点击关键帧跳转到视频对应位置播放。

## 第一阶段功能

- 仅接收 MP4 视频上传
- FFmpeg 在 `0、5、10……` 秒提取画面
- SQLite 保存视频信息、画面路径和毫秒级时间点
- 左侧浏览关键帧，右侧播放原视频
- 点击关键帧后跳转到对应时间并播放

本阶段明确不包含 AI 画面理解、ASR、Embedding、自然语言搜索、登录权限、NAS 同步和自动剪辑。

## 环境

- Git 2.53.0.windows.3
- Python 3.14.7
- FFmpeg（由 `imageio-ffmpeg` 提供本地 Windows 可执行文件）

## 启动

在 PowerShell 中运行：

```powershell
.\.tools\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>。

若使用系统 Python，可创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## 测试

```powershell
.\.tools\Python314\python.exe -m pytest -q
```

测试会使用 FFmpeg 生成一段真实的 12 秒 MP4，再验证上传、抽帧、SQLite 时间点和页面访问。

## 数据目录

- `data/uploads/`：上传的原视频
- `data/frames/`：按视频分目录保存的关键帧
- `data/media.db`：SQLite 索引数据库

这些运行数据默认不会提交到 Git。

