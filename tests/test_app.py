from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import Database
from app.video_service import extraction_timestamps, resolve_ffmpeg
from app.vision_provider import (
    VisionAnalysis,
    VisionProviderError,
    VisionResult,
    validate_vision_result,
)


class FakeVisionProvider:
    model = "fake-vision-model"

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.failures_remaining = 0

    async def analyze(self, image_path: Path) -> VisionAnalysis:
        self.calls.append(image_path)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise VisionProviderError("模拟单帧识别失败")
        result = VisionResult(
            summary=f"测试画面 {image_path.stem}",
            subjects=["测试图形"],
            actions=["移动"],
            scene=["测试背景"],
            shot_type=["横屏", "全景"],
            ocr_text=[],
            confidence=0.95,
        )
        return VisionAnalysis(
            result=result,
            raw_text=result.model_dump_json(),
            model=self.model,
            duration_ms=25,
            input_tokens=100,
            output_tokens=30,
            total_tokens=130,
        )


def make_test_video(path: Path, duration_seconds: int = 12) -> None:
    result = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=25:duration={duration_seconds}",
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


@pytest.fixture()
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upload_dir = tmp_path / "uploads"
    frame_dir = tmp_path / "frames"
    upload_dir.mkdir()
    frame_dir.mkdir()
    test_database = Database(tmp_path / "test.db")
    fake_provider = FakeVisionProvider()

    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "FRAME_DIR", frame_dir)
    monkeypatch.setattr(main, "database", test_database)
    monkeypatch.setattr(main, "vision_provider_factory", lambda: fake_provider)

    with TestClient(main.app) as client:
        yield client, test_database, upload_dir, frame_dir, fake_provider


def upload_test_video(client: TestClient, source: Path) -> dict:
    with source.open("rb") as video:
        response = client.post(
            "/api/videos",
            files={"file": (source.name, video, "video/mp4")},
        )
    assert response.status_code == 201, response.text
    return response.json()


def test_extraction_timestamps_are_exact_five_second_points() -> None:
    assert extraction_timestamps(12_040) == [0, 5_000, 10_000]
    assert extraction_timestamps(5_000) == [0]


def test_real_mp4_upload_extracts_frames_and_persists_timestamps(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, upload_dir, frame_dir, _ = isolated_app
    source = tmp_path / "县融媒测试素材.mp4"
    make_test_video(source)
    payload = upload_test_video(client, source)
    assert payload["original_name"] == source.name
    assert [frame["timestamp_ms"] for frame in payload["frames"]] == [0, 5_000, 10_000]
    assert all(Path(frame_dir / payload["id"] / Path(frame["image_url"]).name).is_file() for frame in payload["frames"])
    assert len(list(upload_dir.glob("*.mp4"))) == 1

    with sqlite3.connect(test_database.path) as connection:
        stored = connection.execute(
            "SELECT timestamp_ms FROM frames ORDER BY timestamp_ms"
        ).fetchall()
    assert stored == [(0,), (5_000,), (10_000,)]

    page = client.get("/")
    assert page.status_code == 200
    assert "县媒智搜" in page.text
    assert payload["video_url"] in page.text


def test_rejects_non_mp4(isolated_app, tmp_path: Path) -> None:
    client, *_ = isolated_app
    response = client.post(
        "/api/videos",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 415


def test_vision_json_validation_accepts_fenced_json() -> None:
    result = validate_vision_result(
        """```json
        {"summary":"道路画面","subjects":["车辆"],"actions":["行驶"],
        "scene":["道路"],"shot_type":["横屏"],"ocr_text":[],"confidence":0.9}
        ```"""
    )
    assert result.summary == "道路画面"
    assert result.confidence == 0.9


def test_vision_analysis_persists_results_and_skips_success(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, _, _, fake_provider = isolated_app
    source = tmp_path / "vision-test.mp4"
    make_test_video(source)
    uploaded = upload_test_video(client, source)

    started = client.post(
        f"/api/videos/{uploaded['id']}/vision/start", json={"force": False}
    )
    assert started.status_code == 200
    assert started.json()["vision_progress"]["completed"] == 0

    for expected_completed in (1, 2, 3):
        response = client.post(f"/api/videos/{uploaded['id']}/vision/next")
        assert response.status_code == 200
        assert response.json()["video"]["vision_progress"]["completed"] == expected_completed

    persisted = client.get(f"/api/videos/{uploaded['id']}").json()
    assert persisted["vision_progress"] == {
        "total": 3,
        "completed": 3,
        "success": 3,
        "failed": 0,
        "processing": 0,
        "pending": 0,
        "done": True,
    }
    assert all(frame["vision_result"]["summary"] for frame in persisted["frames"])
    assert all(frame["vision_total_tokens"] == 130 for frame in persisted["frames"])
    assert len(fake_provider.calls) == 3

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    duplicate = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    assert duplicate["processed"] is False
    assert len(fake_provider.calls) == 3

    page = client.get("/")
    assert "AI识别画面" in page.text
    assert "fake-vision-model" in page.text

    forced = client.post(
        f"/api/videos/{uploaded['id']}/vision/start", json={"force": True}
    ).json()
    assert forced["vision_progress"]["pending"] == 3

    with sqlite3.connect(test_database.path) as connection:
        statuses = connection.execute(
            "SELECT vision_status FROM frames ORDER BY timestamp_ms"
        ).fetchall()
    assert statuses == [("pending",), ("pending",), ("pending",)]


def test_single_vision_failure_does_not_stop_remaining_frames(
    isolated_app, tmp_path: Path
) -> None:
    client, _, _, _, fake_provider = isolated_app
    fake_provider.failures_remaining = 1
    source = tmp_path / "partial-failure.mp4"
    make_test_video(source, duration_seconds=6)
    uploaded = upload_test_video(client, source)

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    first = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    second = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()

    assert first["video"]["vision_progress"]["failed"] == 1
    assert second["done"] is True
    assert second["video"]["vision_progress"]["success"] == 1
    assert second["video"]["vision_progress"]["failed"] == 1
