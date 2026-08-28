from __future__ import annotations

import json
import sqlite3
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import VisionConfigurationError, VisionSettings
from app.database import Database
from app.search_service import SearchService, SearchValidationError
from app.video_service import VideoProcessingError, extract_frames
from app.vision_provider import DeepSeekVisionProvider, VisionProvider
from app.vision_service import VisionAnalysisService


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
FRAME_DIR = DATA_DIR / "frames"
DATABASE_PATH = DATA_DIR / "media.db"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024

database = Database(DATABASE_PATH)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def create_vision_provider() -> VisionProvider:
    settings = VisionSettings.from_environment(BASE_DIR / ".env")
    return DeepSeekVisionProvider(settings)


vision_provider_factory: Callable[[], VisionProvider] = create_vision_provider


class VisionStartRequest(BaseModel):
    force: bool = False


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    database.initialize()
    yield


app = FastAPI(title="县媒智搜", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/videos", StaticFiles(directory=UPLOAD_DIR), name="videos")
app.mount("/media/frames", StaticFiles(directory=FRAME_DIR), name="frames")


def serialize_video(record: dict | None) -> dict | None:
    if record is None:
        return None
    video = record["video"]
    video_id = video["id"]
    serialized_frames = []
    for frame in record["frames"]:
        vision_result = None
        if frame.get("vision_result_json"):
            try:
                vision_result = json.loads(frame["vision_result_json"])
            except json.JSONDecodeError:
                vision_result = None
        serialized_frames.append(
            {
                "timestamp_ms": frame["timestamp_ms"],
                "timestamp_seconds": frame["timestamp_ms"] / 1000,
                "image_url": f"/media/frames/{video_id}/{frame['image_name']}",
                "vision_status": frame.get("vision_status") or "pending",
                "vision_model": frame.get("vision_model"),
                "vision_analyzed_at": frame.get("vision_analyzed_at"),
                "vision_result": vision_result,
                "vision_error": frame.get("vision_error"),
                "vision_duration_ms": frame.get("vision_duration_ms"),
                "vision_input_tokens": frame.get("vision_input_tokens"),
                "vision_output_tokens": frame.get("vision_output_tokens"),
                "vision_total_tokens": frame.get("vision_total_tokens"),
            }
        )

    statuses = [frame["vision_status"] for frame in serialized_frames]
    completed = sum(status in {"success", "failed"} for status in statuses)
    return {
        "id": video_id,
        "original_name": video["original_name"],
        "duration_ms": video["duration_ms"],
        "duration_seconds": video["duration_ms"] / 1000,
        "uploaded_at": video["uploaded_at"],
        "video_url": f"/media/videos/{video['stored_name']}",
        "frames": serialized_frames,
        "vision_progress": {
            "total": len(serialized_frames),
            "completed": completed,
            "success": statuses.count("success"),
            "failed": statuses.count("failed"),
            "processing": statuses.count("processing"),
            "pending": statuses.count("pending"),
            "done": bool(serialized_frames) and completed == len(serialized_frames),
        },
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    latest = serialize_video(database.get_latest_video())
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"initial_video": latest},
    )


@app.get("/api/videos/latest")
async def latest_video() -> dict:
    video = serialize_video(database.get_latest_video())
    if video is None:
        raise HTTPException(status_code=404, detail="尚未上传视频。")
    return video


@app.get("/api/search")
async def search_frames(q: str = "", limit: int = 20) -> dict:
    try:
        response = SearchService(database).search(q, limit=limit)
    except SearchValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="搜索索引暂时不可用。") from exc
    return {
        "query": response.query,
        "count": response.count,
        "elapsed_ms": response.elapsed_ms,
        "backend": response.backend,
        "results": response.results,
    }


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str) -> dict:
    video = serialize_video(database.get_video(video_id))
    if video is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    return video


@app.post("/api/videos/{video_id}/vision/start")
async def start_vision_analysis(video_id: str, request: VisionStartRequest) -> dict:
    if database.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    try:
        vision_provider_factory()
    except VisionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    database.reset_vision(video_id, force=request.force)
    return serialize_video(database.get_video(video_id))


@app.post("/api/videos/{video_id}/vision/next")
async def analyze_next_frame(video_id: str) -> dict:
    if database.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    try:
        provider = vision_provider_factory()
    except VisionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    service = VisionAnalysisService(
        database=database,
        frame_root=FRAME_DIR,
        provider=provider,
    )
    processed = await service.process_next(video_id)
    video = serialize_video(database.get_video(video_id))
    return {
        "processed": processed,
        "done": video["vision_progress"]["done"],
        "video": video,
    }


@app.post("/api/videos", status_code=status.HTTP_201_CREATED)
async def upload_video(file: UploadFile = File(...)) -> dict:
    original_name = Path(file.filename or "").name
    if not original_name or Path(original_name).suffix.lower() != ".mp4":
        raise HTTPException(status_code=415, detail="目前仅支持 MP4 视频。")

    video_id = uuid4().hex
    stored_name = f"{video_id}.mp4"
    video_path = UPLOAD_DIR / stored_name
    output_dir = FRAME_DIR / video_id
    bytes_written = 0

    try:
        with video_path.open("xb") as destination:
            while chunk := await file.read(COPY_CHUNK_BYTES):
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="视频不能超过 2 GB。")
                destination.write(chunk)

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="上传文件不能为空。")

        duration_ms, frames = extract_frames(video_path, output_dir)
        uploaded_at = datetime.now(timezone.utc).isoformat()
        database.insert_video(
            video_id=video_id,
            original_name=original_name,
            stored_name=stored_name,
            duration_ms=duration_ms,
            uploaded_at=uploaded_at,
            frames=[(frame.timestamp_ms, frame.image_name) for frame in frames],
        )
    except HTTPException:
        video_path.unlink(missing_ok=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except VideoProcessingError as exc:
        video_path.unlink(missing_ok=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()

    return serialize_video(database.get_video(video_id))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
