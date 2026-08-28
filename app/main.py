from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import Database
from app.video_service import VideoProcessingError, extract_frames


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
FRAME_DIR = DATA_DIR / "frames"
DATABASE_PATH = DATA_DIR / "media.db"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024

database = Database(DATABASE_PATH)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    database.initialize()
    yield


app = FastAPI(title="县媒智搜", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/videos", StaticFiles(directory=UPLOAD_DIR), name="videos")
app.mount("/media/frames", StaticFiles(directory=FRAME_DIR), name="frames")


def serialize_video(record: dict | None) -> dict | None:
    if record is None:
        return None
    video = record["video"]
    video_id = video["id"]
    return {
        "id": video_id,
        "original_name": video["original_name"],
        "duration_ms": video["duration_ms"],
        "duration_seconds": video["duration_ms"] / 1000,
        "uploaded_at": video["uploaded_at"],
        "video_url": f"/media/videos/{video['stored_name']}",
        "frames": [
            {
                "timestamp_ms": frame["timestamp_ms"],
                "timestamp_seconds": frame["timestamp_ms"] / 1000,
                "image_url": f"/media/frames/{video_id}/{frame['image_name']}",
            }
            for frame in record["frames"]
        ],
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


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str) -> dict:
    video = serialize_video(database.get_video(video_id))
    if video is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    return video


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
