# 县媒智搜——县级融媒体声画智能检索系统

面向县级融媒体中心编辑的历史素材检索系统。当前在第一阶段视频上传与关键帧索引之上，增加第二阶段关键帧画面理解能力。

## 第一阶段功能

- 仅接收 MP4 视频上传
- FFmpeg 在 `0、5、10……` 秒提取画面
- SQLite 保存视频信息、画面路径和毫秒级时间点
- 左侧浏览关键帧，右侧播放原视频
- 点击关键帧后跳转到对应时间并播放

以上为已经封存的第一阶段能力。

## 第二阶段功能

- 点击“AI识别画面”后逐帧调用独立视觉 Provider
- Base64 在后端直接传递图片，不依赖公开图片 URL
- SQLite 保存待处理、处理中、成功、失败状态和结构化结果
- 页面逐帧显示摘要、主体、动作、场景、镜头类型与 OCR 文字
- 成功帧默认跳过，仅在用户确认“重新分析”后再次调用
- 单帧失败不会阻断后续画面，测试使用 Fake Provider

第二阶段仍不包含 ASR、Embedding、自然语言搜索、多模型切换界面、NAS、自动扫描或复杂统计。

## DeepSeek 配置

复制 `.env.example` 为本地 `.env`，只在本机填写 API Key：

```ini
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_VISION_MODEL=deepseek-v4-flash-vision-exp
```

`.env` 已被 Git 忽略，API Key 只在后端读取。请勿将真实密钥写入源码、测试或聊天内容。

> 注意：截至 2026-08-28，DeepSeek 官方公开文档尚未列出上述实验视觉模型，且公开 API 文档未声明支持图片输入。真实调用能否成功取决于账号是否具备该实验模型权限。

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
