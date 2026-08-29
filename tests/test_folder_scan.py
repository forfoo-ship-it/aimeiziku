from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import Database
from app.folder_scan_service import FolderScanService, infer_media_created_at
from app.video_service import resolve_ffmpeg
from app.vision_provider import VisionAnalysis, VisionResult


class FolderFakeVisionProvider:
    model = "folder-fake-vision"

    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def analyze(self, image_path: Path) -> VisionAnalysis:
        self.calls.append(image_path)
        result = VisionResult(
            summary="文件夹扫描测试画面",
            subjects=["测试主体"],
            actions=["测试动作"],
            scene=["测试场景"],
            shot_type=["横屏"],
            ocr_text=["自动索引"],
            confidence=0.95,
        )
        return VisionAnalysis(
            result=result,
            raw_text=result.model_dump_json(),
            model=self.model,
            duration_ms=5,
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )


def make_folder_video(path: Path, duration_seconds: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=20:duration={duration_seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def create_folder_record(
    database: Database, source_dir: Path, *, auto_analyze: bool
) -> dict:
    return database.upsert_watch_folder(
        path=str(source_dir.resolve()),
        auto_analyze=auto_analyze,
        scan_interval_seconds=60,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_media_date_accepts_compact_camera_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "DJI_20240713121746_0150_D.MP4"
    assert infer_media_created_at(path, 0).startswith("2024-07-13")


def test_folder_scan_recurses_dates_and_skips_duplicate_without_api(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    make_folder_video(source_dir / "活动" / "DJI_20240517_demo.mp4")
    database = Database(tmp_path / "media.db")
    database.initialize()
    provider = FolderFakeVisionProvider()
    service = FolderScanService(
        database=database,
        upload_dir=tmp_path / "uploads",
        frame_dir=tmp_path / "frames",
        provider_factory=lambda: provider,
    )
    folder = create_folder_record(database, source_dir, auto_analyze=False)

    async def scenario() -> None:
        first = service.start_scan(folder["id"])
        first_done = await service.wait(first["id"])
        assert first_done["status"] == "completed"
        assert first_done["imported"] == 1
        assert first_done["skipped"] == 0

        second = service.start_scan(folder["id"])
        second_done = await service.wait(second["id"])
        assert second_done["status"] == "completed"
        assert second_done["imported"] == 0
        assert second_done["skipped"] == 1

    asyncio.run(scenario())
    videos = database.list_videos()
    assert len(videos) == 1
    assert videos[0]["media_created_at"].startswith("2024-05-17")
    assert videos[0]["source_kind"] == "folder"
    assert videos[0]["source_folder"] == str(
        (source_dir / "活动").resolve()
    )
    assert videos[0]["index_status"] == "pending_analysis"
    assert len(database.get_video(videos[0]["id"])["frames"]) == 2
    assert provider.calls == []


def test_auto_analysis_indexes_once_and_marks_video_indexed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    make_folder_video(source_dir / "county-event-20240603.mp4")
    database = Database(tmp_path / "media.db")
    database.initialize()
    provider = FolderFakeVisionProvider()
    service = FolderScanService(
        database=database,
        upload_dir=tmp_path / "uploads",
        frame_dir=tmp_path / "frames",
        provider_factory=lambda: provider,
    )
    folder = create_folder_record(database, source_dir, auto_analyze=True)

    async def scenario() -> None:
        first = service.start_scan(folder["id"])
        assert (await service.wait(first["id"]))["imported"] == 1
        second = service.start_scan(folder["id"])
        assert (await service.wait(second["id"]))["skipped"] == 1

    asyncio.run(scenario())
    videos = database.list_videos()
    assert len(videos) == 1
    assert videos[0]["index_status"] == "indexed"
    assert database.search_index_count() == 2
    assert len(provider.calls) == 2


def test_watch_folder_api_reports_progress_and_stops_without_deleting_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "watched"
    make_folder_video(source_dir / "news-20240708.mp4")
    database = Database(tmp_path / "api.db")
    upload_dir = tmp_path / "uploads"
    frame_dir = tmp_path / "frames"
    provider = FolderFakeVisionProvider()
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "FRAME_DIR", frame_dir)
    monkeypatch.setattr(main, "vision_provider_factory", lambda: provider)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/watch-folders",
            json={
                "path": str(source_dir),
                "auto_analyze": False,
                "scan_interval_seconds": 60,
            },
        )
        assert created.status_code == 201, created.text
        folder_id = created.json()["folder"]["id"]
        job_id = created.json()["job"]["id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/scan-jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert job["status"] == "completed"
        assert job["progress_percent"] == 100
        assert job["imported"] == 1
        folders = client.get("/api/watch-folders").json()["folders"]
        assert folders[0]["latest_job"]["status"] == "completed"
        stopped = client.delete(f"/api/watch-folders/{folder_id}")
        assert stopped.status_code == 204
        assert client.get("/api/watch-folders").json()["folders"] == []
        assert client.get("/api/videos").json()["count"] == 1

    assert provider.calls == []
