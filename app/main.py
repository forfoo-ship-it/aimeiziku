from __future__ import annotations

import asyncio
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
from app.folder_scan_service import (
    FolderScanError,
    FolderScanService,
    infer_media_created_at,
)
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
folder_scan_service: FolderScanService | None = None


class VisionStartRequest(BaseModel):
    force: bool = False


class WatchFolderRequest(BaseModel):
    path: str
    auto_analyze: bool = False
    scan_interval_seconds: int = 60


class ScanStartRequest(BaseModel):
    auto_analyze: bool | None = None


async def folder_monitor_loop(service: FolderScanService) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for folder in database.list_watch_folders(enabled_only=True):
            last_scan_at = folder.get("last_scan_at")
            due = last_scan_at is None
            if last_scan_at:
                try:
                    last_scan = datetime.fromisoformat(last_scan_at)
                    due = (now - last_scan).total_seconds() >= int(
                        folder["scan_interval_seconds"]
                    )
                except (TypeError, ValueError):
                    due = True
            if due:
                try:
                    service.start_scan(int(folder["id"]))
                except (FolderScanError, sqlite3.Error):
                    continue
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global folder_scan_service
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    database.initialize()
    folder_scan_service = FolderScanService(
        database=database,
        upload_dir=UPLOAD_DIR,
        frame_dir=FRAME_DIR,
        provider_factory=vision_provider_factory,
    )
    monitor_task = asyncio.create_task(
        folder_monitor_loop(folder_scan_service), name="folder-monitor"
    )
    try:
        yield
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await folder_scan_service.shutdown()
        folder_scan_service = None


app = FastAPI(title="AI媒资库", version="0.4.0", lifespan=lifespan)
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
        "media_created_at": video.get("media_created_at") or video["uploaded_at"],
        "source_kind": video.get("source_kind") or "upload",
        "index_status": video.get("index_status") or "pending_analysis",
        "index_error": video.get("index_error"),
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


def serialize_scan_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    total = int(job.get("discovered") or 0)
    processed = int(job.get("processed") or 0)
    return {
        **job,
        "auto_analyze": bool(job.get("auto_analyze")),
        "progress_percent": round(processed / total * 100) if total else 0,
    }


def video_thumbnail_url(video: dict) -> str | None:
    image_name = video.get("first_frame_image_name")
    if not image_name or Path(image_name).name != image_name:
        return None
    if not (FRAME_DIR / video["id"] / image_name).is_file():
        return None
    return f"/media/frames/{video['id']}/{image_name}"


def require_folder_scan_service() -> FolderScanService:
    if folder_scan_service is None:
        raise HTTPException(status_code=503, detail="文件夹监测服务尚未启动。")
    return folder_scan_service


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


@app.get("/api/videos")
async def list_videos(limit: int = 200, offset: int = 0) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1 至 500 之间。")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能小于 0。")
    videos = database.list_videos(limit=limit, offset=offset)
    total = database.count_videos()
    return {
        "count": len(videos),
        "total": total,
        "offset": offset,
        "has_more": offset + len(videos) < total,
        "videos": [
            {
                "id": video["id"],
                "original_name": video["original_name"],
                "media_created_at": video.get("media_created_at")
                or video["uploaded_at"],
                "source_kind": video.get("source_kind") or "upload",
                "index_status": video.get("index_status") or "pending_analysis",
                "index_error": video.get("index_error"),
                "thumbnail_url": video_thumbnail_url(video),
                "frame_count": int(video.get("frame_count") or 0),
                "success_count": int(video.get("success_count") or 0),
                "failed_count": int(video.get("failed_count") or 0),
                "processing_count": int(video.get("processing_count") or 0),
                "pending_count": int(video.get("pending_count") or 0),
            }
            for video in videos
        ],
    }


@app.get("/api/watch-folders")
async def list_watch_folders() -> dict:
    folders = database.list_watch_folders()
    return {
        "folders": [
            {
                **folder,
                "enabled": bool(folder["enabled"]),
                "auto_analyze": bool(folder["auto_analyze"]),
                "latest_job": serialize_scan_job(
                    {
                        "id": folder["latest_job_id"],
                        "status": folder["latest_job_status"],
                        "auto_analyze": folder["auto_analyze"],
                        "discovered": folder["discovered"],
                        "processed": folder["processed"],
                        "imported": folder["imported"],
                        "skipped": folder["skipped"],
                        "failed": folder["failed"],
                        "current_file": folder["current_file"],
                        "current_stage": folder["current_stage"],
                        "error": folder["job_error"],
                        "started_at": folder["started_at"],
                        "finished_at": folder["finished_at"],
                    }
                    if folder["latest_job_id"]
                    else None
                ),
            }
            for folder in folders
        ]
    }


def resolve_watch_folder(raw_path: str) -> Path:
    cleaned = raw_path.strip()
    if not cleaned or len(cleaned) > 2000:
        raise HTTPException(status_code=400, detail="请输入有效的本地文件夹路径。")
    try:
        folder_path = Path(cleaned).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="文件夹不存在或无法访问。") from exc
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="指定路径不是文件夹。")
    data_path = DATA_DIR.resolve()
    if folder_path == data_path or data_path.is_relative_to(folder_path):
        raise HTTPException(
            status_code=400,
            detail="不能监测项目 data 目录或它的上级目录，以免重复导入运行文件。",
        )
    return folder_path


@app.post("/api/watch-folders", status_code=status.HTTP_201_CREATED)
async def add_watch_folder(request: WatchFolderRequest) -> dict:
    if request.scan_interval_seconds < 15 or request.scan_interval_seconds > 3600:
        raise HTTPException(status_code=400, detail="扫描间隔必须在 15 到 3600 秒之间。")
    folder_path = resolve_watch_folder(request.path)
    folder = database.upsert_watch_folder(
        path=str(folder_path),
        auto_analyze=request.auto_analyze,
        scan_interval_seconds=request.scan_interval_seconds,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        job = require_folder_scan_service().start_scan(int(folder["id"]))
    except FolderScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"folder": folder, "job": serialize_scan_job(job)}


@app.post("/api/watch-folders/{folder_id}/scan")
async def scan_watch_folder(folder_id: int, request: ScanStartRequest) -> dict:
    try:
        job = require_folder_scan_service().start_scan(
            folder_id, auto_analyze=request.auto_analyze
        )
    except FolderScanError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_scan_job(job)


@app.delete("/api/watch-folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watch_folder(folder_id: int) -> None:
    if database.get_active_scan_job(folder_id):
        raise HTTPException(status_code=409, detail="目录正在扫描，请等待完成后再停止监测。")
    if not database.delete_watch_folder(folder_id):
        raise HTTPException(status_code=404, detail="未找到该监测目录。")


@app.get("/api/scan-jobs/{job_id}")
async def get_scan_job(job_id: str) -> dict:
    job = database.get_scan_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="未找到该扫描任务。")
    return serialize_scan_job(job)


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
            media_created_at=infer_media_created_at(
                Path(original_name), video_path.stat().st_mtime
            ),
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
