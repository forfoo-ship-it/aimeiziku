# AGENTS.md

## 项目边界

- 当前阶段只实现 MP4 上传、FFmpeg 每 5 秒抽帧、时间点索引和播放器跳转。
- 技术栈固定为 Python、FastAPI、SQLite、原生 HTML/CSS/JavaScript。
- 不接入 DeepSeek、ASR、Embedding、登录权限、NAS 同步或自动剪辑。

## 开发约定

- 保持依赖精简，所有运行数据写入 `data/`。
- 上传文件必须验证为 MP4，文件名不得直接用于磁盘路径。
- 修改抽帧逻辑后，必须用真实 MP4 验证时间点和播放器跳转数据。
- 提交前运行 `pytest`，并确认服务可以正常启动。

