from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import VisionSettings


SYSTEM_PROMPT = """你是县级融媒体历史素材的画面分析助手。
只描述能够从当前画面直接确认的内容，不猜测人物姓名、具体地点、事件背景或因果关系。
无法确认的字段返回空数组。必须只返回一个 JSON 对象，不要输出 Markdown。"""

USER_PROMPT = """分析这张视频关键帧，按以下结构返回：
{
  "summary": "画面总体描述",
  "subjects": ["主要人物或物体"],
  "actions": ["主要动作"],
  "scene": ["场景和环境"],
  "shot_type": ["横屏", "近景"],
  "ocr_text": ["画面中能够确认的文字"],
  "confidence": 0.9
}
confidence 必须是 0 到 1 之间的数字。"""


class VisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    subjects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    scene: list[str] = Field(default_factory=list)
    shot_type: list[str] = Field(default_factory=list)
    ocr_text: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class VisionAnalysis:
    result: VisionResult
    raw_text: str
    model: str
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class VisionProviderError(RuntimeError):
    """视觉服务调用失败。"""


class VisionResponseValidationError(VisionProviderError):
    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


class VisionProvider(Protocol):
    model: str

    async def analyze(self, image_path: Path) -> VisionAnalysis: ...


def _json_candidate(raw_text: str) -> str:
    candidate = raw_text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        candidate = candidate[first_newline + 1 :] if first_newline >= 0 else candidate
        if candidate.endswith("```"):
            candidate = candidate[:-3]
    start = candidate.find("{")
    end = candidate.rfind("}")
    return candidate[start : end + 1] if start >= 0 and end > start else candidate


def validate_vision_result(raw_text: str) -> VisionResult:
    try:
        payload = json.loads(_json_candidate(raw_text))
        return VisionResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise VisionResponseValidationError(
            f"视觉模型返回的 JSON 无法校验：{exc}", raw_text
        ) from exc


class DeepSeekVisionProvider:
    def __init__(self, settings: VisionSettings) -> None:
        self.settings = settings
        self.model = settings.model

    async def analyze(self, image_path: Path) -> VisionAnalysis:
        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            },
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": 800,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.settings.timeout_seconds, connect=10.0)
        started = time.perf_counter()

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await self._post_with_retries(client, payload, headers)

        duration_ms = round((time.perf_counter() - started) * 1000)
        try:
            body = response.json()
            raw_text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VisionProviderError("视觉服务响应缺少有效的结果内容。") from exc

        if not isinstance(raw_text, str) or not raw_text.strip():
            raise VisionResponseValidationError("视觉服务返回了空内容。", str(raw_text))

        result = validate_vision_result(raw_text)
        usage = body.get("usage") or {}
        return VisionAnalysis(
            result=result,
            raw_text=raw_text,
            model=str(body.get("model") or self.model),
            duration_ms=duration_ms,
            input_tokens=_optional_int(usage.get("prompt_tokens")),
            output_tokens=_optional_int(usage.get("completion_tokens")),
            total_tokens=_optional_int(usage.get("total_tokens")),
        )

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        payload: dict,
        headers: dict[str, str],
    ) -> httpx.Response:
        endpoint = f"{self.settings.base_url}/chat/completions"
        attempts = self.settings.max_retries + 1

        for attempt in range(attempts):
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= attempts:
                    raise VisionProviderError(
                        f"视觉服务网络请求失败，已重试 {self.settings.max_retries} 次：{exc}"
                    ) from exc
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code < 400:
                return response
            if response.status_code not in {408, 429} and response.status_code < 500:
                raise VisionProviderError(
                    f"视觉服务拒绝请求（HTTP {response.status_code}）："
                    f"{response.text[:300]}"
                )
            if attempt + 1 >= attempts:
                raise VisionProviderError(
                    f"视觉服务暂时不可用（HTTP {response.status_code}），"
                    f"已重试 {self.settings.max_retries} 次。"
                )
            await asyncio.sleep(0.5 * (2**attempt))

        raise VisionProviderError("视觉服务请求未完成。")


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None

