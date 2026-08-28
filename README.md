# 县媒智搜——县级融媒体声画智能检索系统

面向县级融媒体中心编辑的历史素材检索系统。当前已完成视频关键帧索引、AI 画面理解，以及基于 SQLite 画面索引的自然语言检索。

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

## 第三阶段功能

- 首页用自然语言检索所有已经成功识别的关键帧
- SQLite 支持 FTS5 时使用全文索引，不支持时自动回退为安全的普通全文匹配
- 对主体、动作、OCR、场景、镜头类型、摘要、文件名和时间点进行加权排序
- 支持“打鼓/击鼓”“横版/横屏”“航拍/俯瞰”等可配置同义词召回
- 搜索结果显示结构化画面信息、相关度、命中字段和命中理由
- 点击结果后自动切换对应原视频，跳至关键帧时间点并播放
- AI 识别成功或重新识别后自动更新索引；重复更新不会产生重复数据
- 搜索只读取 SQLite，不提取关键帧，也不会调用 DeepSeek 或产生新的视觉识别费用

当前仍不包含 ASR、同期声检索、Embedding、向量数据库、多模型切换界面、NAS、自动扫描、权限、自动剪辑或公网部署。

## 画面搜索

在首页顶部输入 2 至 200 个字符，或点击示例词开始搜索。接口也可直接调用：

```text
GET /api/search?q=龙舟鼓手击鼓的横屏镜头&limit=20
```

`limit` 可设为 1 至 50。返回结果中的 `score` 是检索相关度，不是 AI 识别准确率。无结果时返回空列表；空查询返回 400。

已有第二阶段数据会在应用启动时自动补建索引。开发维护时可手动重建全部搜索索引：

```powershell
.\.tools\Python314\python.exe -m app.rebuild_search_index
```

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

测试会使用 FFmpeg 生成一段真实的 12 秒 MP4，验证上传、抽帧、SQLite 时间点和页面访问；搜索测试使用固定模拟识别数据，不连接 DeepSeek，也不消耗真实 API 额度。

## 数据目录

- `data/uploads/`：上传的原视频
- `data/frames/`：按视频分目录保存的关键帧
- `data/media.db`：SQLite 索引数据库

这些运行数据默认不会提交到 Git。
