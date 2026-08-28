from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class VisionConfigurationError(RuntimeError):
    """视觉识别环境配置缺失或无效。"""


def load_env_file(path: Path) -> None:
    """读取简单 KEY=VALUE 配置，但不覆盖系统已有环境变量。"""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class VisionSettings:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 45.0
    max_retries: int = 2

    @classmethod
    def from_environment(cls, env_path: Path) -> "VisionSettings":
        load_env_file(env_path)
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        base_url = os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).strip()
        model = os.environ.get(
            "DEEPSEEK_VISION_MODEL", "deepseek-v4-flash-vision-exp"
        ).strip()

        if not api_key:
            raise VisionConfigurationError(
                "尚未配置 DEEPSEEK_API_KEY，请在本地 .env 中填写后重试。"
            )
        if not base_url.startswith(("https://", "http://")):
            raise VisionConfigurationError("DEEPSEEK_BASE_URL 必须是有效的 HTTP 地址。")
        if not model:
            raise VisionConfigurationError("DEEPSEEK_VISION_MODEL 不能为空。")

        return cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
        )

