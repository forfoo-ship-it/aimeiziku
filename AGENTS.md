# AGENTS.md

## 项目边界

- 当前阶段在第一阶段能力之上，只增加 DeepSeek 关键帧画面理解、结构化入库和页面展示。
- 技术栈固定为 Python、FastAPI、SQLite、原生 HTML/CSS/JavaScript。
- 不接入 ASR、自然语言搜索、Embedding、登录权限、NAS 同步或自动剪辑。

## 开发约定

- 保持依赖精简，所有运行数据写入 `data/`。
- 上传文件必须验证为 MP4，文件名不得直接用于磁盘路径。
- 修改抽帧逻辑后，必须用真实 MP4 验证时间点和播放器跳转数据。
- 视觉 API 必须通过独立 Provider 调用；测试只用 Fake Provider，禁止消耗真实 API。
- 不得记录 API Key 或完整 Base64，成功帧默认不得重复调用 API。
- 提交前运行 `pytest`，并确认服务可以正常启动。
