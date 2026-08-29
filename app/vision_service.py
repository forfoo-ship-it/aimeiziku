from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from app.database import Database
from app.vision_provider import (
    VisionProvider,
    VisionProviderError,
    VisionResponseValidationError,
)


class VisionAnalysisService:
    def __init__(
        self,
        *,
        database: Database,
        frame_root: Path,
        provider: VisionProvider,
    ) -> None:
        self.database = database
        self.frame_root = frame_root
        self.provider = provider

    async def process_next(self, video_id: str) -> bool:
        frame = self.database.claim_next_vision_frame(video_id)
        if frame is None:
            return False
        await self._process_claimed_frame(video_id, frame)
        return True

    async def process_duplicate(self, video_id: str, frame_id: int) -> bool:
        frame = self.database.claim_duplicate_vision_frame(video_id, frame_id)
        if frame is None:
            return False
        await self._process_claimed_frame(video_id, frame)
        return True

    async def _process_claimed_frame(self, video_id: str, frame: dict) -> None:
        started = time.perf_counter()
        analyzed_at = datetime.now(timezone.utc).isoformat()
        image_path = self.frame_root / video_id / frame["image_name"]

        try:
            if not image_path.is_file():
                raise VisionProviderError("关键帧文件不存在，无法识别。")
            analysis = await self.provider.analyze(image_path)
            self.database.save_vision_success(
                frame_id=frame["id"],
                model=analysis.model,
                analyzed_at=analyzed_at,
                result_json=analysis.result.model_dump_json(),
                raw_text=analysis.raw_text,
                duration_ms=analysis.duration_ms,
                input_tokens=analysis.input_tokens,
                output_tokens=analysis.output_tokens,
                total_tokens=analysis.total_tokens,
            )
        except VisionResponseValidationError as exc:
            self.database.save_vision_failure(
                frame_id=frame["id"],
                model=self.provider.model,
                analyzed_at=analyzed_at,
                error=str(exc),
                raw_text=exc.raw_text,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except (VisionProviderError, OSError) as exc:
            self.database.save_vision_failure(
                frame_id=frame["id"],
                model=self.provider.model,
                analyzed_at=analyzed_at,
                error=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
