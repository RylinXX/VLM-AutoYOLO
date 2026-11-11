"""Image/video prediction and model validation routes."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from uuid import UUID

import cv2
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from ...core.config import settings
from ...core.database import SessionLocal, get_db
from ...core.gpu_memory import get_memory_manager
from ...models.train import TrainingJob
from ...models.video import Video
from ...schemas.common import APIResponse
from ...services.frame_utils import draw_frame, predict_frame
from ...services.trainer import predict_trained_model
from ...services.video_service import _ffprobe

router = APIRouter(prefix="/api/v1/train", tags=["predict"])


# ── External model storage ────────────────────────────

_external_models_dir = Path(__file__).resolve().parent.parent.parent.parent / "external_models"
_external_models_dir.mkdir(exist_ok=True)


def _token_path(token: str) -> Path:
    return _external_models_dir / f"{token}.pt"


@router.post("/upload-model")
async def upload_external_model(file: UploadFile = File(...)):
    """Upload an external YOLO .pt model, return a token for MJPEG validation."""
    if not file.filename or not file.filename.endswith(".pt"):
        raise HTTPException(400, "Only .pt files are accepted")

    token = uuid.uuid4().hex
    dst = _token_path(token)
    content = await file.read()
    dst.write_bytes(content)

    return APIResponse(data={"token": token, "fileName": file.filename})


# ── Single image prediction ────────────────────────────


@router.post("/jobs/{job_id}/predict")
async def predict_with_model(
    job_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    conf: float = Form(0.25),
    iou: float = Form(0.45),
):
    """Run inference with a trained YOLO model on an uploaded image."""
    job = db.query(TrainingJob).filter(TrainingJob.id == job_id).first()
    if not job or not job.model_path:
        raise HTTPException(404, "Trained model not found")
    if job.status != "completed":
        raise HTTPException(400, "Training not completed yet")
    if not Path(job.model_path).exists():
        raise HTTPException(404, "Model file not found on disk")

    img_bytes = await file.read()
    try:
        img = Image.open(io.BytesIO(img_bytes))
        result = predict_trained_model(
            job.model_path,
            img,
            device=settings.resolved_device,
            conf=conf,
            iou=iou,
        )
    except Exception as exc:
        raise HTTPException(500, f"Inference failed: {exc}") from exc
    finally:
        get_memory_manager().full_cleanup()

    class_map = {}
    if job.metrics and isinstance(job.metrics, dict):
        class_map = job.metrics.get("class_map", {})

    boxes_camel = [
        {
            "className": b["class_name"],
            "confidence": b["confidence"],
            "x1": b["x1"],
            "y1": b["y1"],
            "x2": b["x2"],
            "y2": b["y2"],
        }
        for b in result["boxes"]
    ]
    return APIResponse(
        data={
            "imageWidth": result["image_width"],
            "imageHeight": result["image_height"],
            "boxes": boxes_camel,
            "modelVariant": job.model_variant,
            "classMap": class_map,
        }
    )


@router.post("/validate-image/{token}")
async def predict_external_model(
    token: str,
    file: UploadFile = File(...),
    conf: float = Form(0.25),
    iou: float = Form(0.45),
):
    """Run inference with an uploaded external YOLO model (via token) on an image."""
    model_path = _token_path(token)
    if not model_path.exists():
        raise HTTPException(404, "Model token not found or expired")

    img_bytes = await file.read()
    try:
        img = Image.open(io.BytesIO(img_bytes))
        result = predict_trained_model(
            str(model_path),
            img,
            device=settings.resolved_device,
            conf=conf,
            iou=iou,
        )
    except Exception as exc:
        raise HTTPException(500, f"Inference failed: {exc}") from exc
    finally:
        get_memory_manager().full_cleanup()

    boxes_camel = [
        {
            "className": b["class_name"],
            "confidence": b["confidence"],
            "x1": b["x1"],
            "y1": b["y1"],
            "x2": b["x2"],
            "y2": b["y2"],
        }
        for b in result["boxes"]
    ]
    return APIResponse(
        data={
            "imageWidth": result["image_width"],
            "imageHeight": result["image_height"],
            "boxes": boxes_camel,
        }
    )


# ── MJPEG video validation ────────────────────────────


@router.get("/validate-mjpeg/{token}/{video_id}")
async def validate_mjpeg_external(
    token: str,
    video_id: UUID,
    conf: float = Query(0.25),
    iou: float = Query(0.45),
):
    """MJPEG validation with an external model (token from upload-model)."""
    model_path = _token_path(token)
    if not model_path.exists():
        raise HTTPException(404, "Model token not found or expired")
    model_path = str(model_path)

    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(404, "Video not found")
        vp = Path(video.file_path)
        if not vp.exists():
            raise HTTPException(404, "Video file not found")
        video_path = str(vp)
    finally:
        db.close()

    meta = _ffprobe(video_path)
    fps = meta["fps"]

    async def mjpeg_stream():
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        buf, frame_num = b"", 0
        try:
            while True:
                chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                if not chunk:
                    break
                buf += chunk
                end = buf.find(b"\xff\xd9")
                if end == -1:
                    continue
                jpg = buf[: end + 2]
                buf = buf[end + 2 :]
                annotated = await asyncio.to_thread(
                    draw_frame,
                    model_path,
                    jpg,
                    settings.resolved_device,
                    conf,
                    iou,
                    frame_num,
                    fps,
                )
                if annotated:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + annotated + b"\r\n")
                    await asyncio.sleep(0)
                frame_num += 1
        except BaseException:
            proc.terminate()
            raise
        finally:
            proc.terminate()
            get_memory_manager().full_cleanup()

    return StreamingResponse(
        mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/validate-mjpeg/{video_id}")
async def validate_mjpeg(
    job_id: UUID,
    video_id: UUID,
    db: Session = Depends(get_db),
    conf: float = Query(0.25),
    iou: float = Query(0.45),
):
    """MJPEG endpoint: process every video frame, draw boxes, stream as MJPEG."""
    job = db.query(TrainingJob).filter(TrainingJob.id == job_id).first()
    if not job or not job.model_path:
        raise HTTPException(404, "Trained model not found")
    if job.status != "completed":
        raise HTTPException(400, "Training not completed")
    if not Path(job.model_path).exists():
        raise HTTPException(404, "Model file not found on disk")

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(404, "Video not found")
    if not Path(video.file_path).exists():
        raise HTTPException(404, "Video file not found on disk")

    video_path = video.file_path
    meta = _ffprobe(video_path)
    fps = meta["fps"]

    async def mjpeg_stream():
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        buf = b""
        frame_num = 0

        try:
            while True:
                chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                if not chunk:
                    break
                buf += chunk
                end = buf.find(b"\xff\xd9")
                if end == -1:
                    continue
                jpg = buf[: end + 2]
                buf = buf[end + 2 :]

                annotated = await asyncio.to_thread(
                    draw_frame,
                    job.model_path,
                    jpg,
                    settings.resolved_device,
                    conf,
                    iou,
                    frame_num,
                    fps,
                )
                if annotated:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + annotated + b"\r\n")
                    await asyncio.sleep(0)
                frame_num += 1
        except BaseException:
            proc.terminate()
            raise
        finally:
            proc.terminate()
            get_memory_manager().full_cleanup()

    return StreamingResponse(
        mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── SSE video prediction ──────────────────────────────


@router.post("/jobs/{job_id}/predict-video-stream")
async def predict_video_stream(
    job_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    conf: float = Form(0.25),
    iou: float = Form(0.45),
):
    """SSE endpoint: process every video frame and stream YOLO results in real-time."""
    job = db.query(TrainingJob).filter(TrainingJob.id == job_id).first()
    if not job or not job.model_path:
        raise HTTPException(404, "Trained model not found")
    if job.status != "completed":
        raise HTTPException(400, "Training not completed")
    if not Path(job.model_path).exists():
        raise HTTPException(404, "Model file not found on disk")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_video:
        tmp_video.write(await file.read())
        tmp_video_path = tmp_video.name

    meta = _ffprobe(tmp_video_path)
    fps = meta["fps"]
    total_frames = meta["total_frames"]

    async def event_stream():
        frame_num = 0

        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-i",
                tmp_video_path,
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None

        buf = b""
        start_time = None
        processed = 0

        try:
            while True:
                chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                if not chunk:
                    break
                buf += chunk
                end = buf.find(b"\xff\xd9")
                if end == -1:
                    continue
                jpg_data = buf[: end + 2]
                buf = buf[end + 2 :]

                if start_time is None:
                    start_time = asyncio.get_running_loop().time()

                result = await asyncio.to_thread(
                    predict_frame,
                    job.model_path,
                    jpg_data,
                    settings.resolved_device,
                    conf,
                    iou,
                )

                timestamp = round(frame_num / fps, 3) if fps > 0 else frame_num
                boxes_camel = [
                    {
                        "className": b["class_name"],
                        "confidence": b["confidence"],
                        "x1": b["x1"],
                        "y1": b["y1"],
                        "x2": b["x2"],
                        "y2": b["y2"],
                    }
                    for b in result["boxes"]
                ]

                event_data = {
                    "frame": frame_num,
                    "fps": fps,
                    "totalFrames": total_frames,
                    "timestamp": timestamp,
                    "imageWidth": result["image_width"],
                    "imageHeight": result["image_height"],
                    "boxes": boxes_camel,
                }
                yield f"data: {json.dumps(event_data)}\n\n"

                frame_num += 1
                processed += 1
                await asyncio.sleep(0)
        except BaseException:
            proc.terminate()
            raise
        finally:
            proc.terminate()
            elapsed = asyncio.get_running_loop().time() - start_time if start_time else 0
            done_data = {"done": True, "frames": processed, "elapsed": round(elapsed, 2)}
            yield f"data: {json.dumps(done_data)}\n\n"
            Path(tmp_video_path).unlink(missing_ok=True)
            get_memory_manager().full_cleanup()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Sync video prediction ──────────────────────────────


@router.post("/jobs/{job_id}/predict-video")
def predict_video(
    job_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    conf: float = Form(0.25),
    iou: float = Form(0.45),
    interval: float = Form(2.0),
    max_frames: int = Form(100),
):
    """Validate model on a video: extract frames, run inference, return results."""
    job = db.query(TrainingJob).filter(TrainingJob.id == job_id).first()
    if not job or not job.model_path:
        raise HTTPException(404, "Trained model not found")
    if job.status != "completed":
        raise HTTPException(400, "Training not completed")
    if not Path(job.model_path).exists():
        raise HTTPException(404, "Model file not found on disk")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_video:
        tmp_video.write(file.file.read())
        tmp_video_path = tmp_video.name

    try:
        meta = _ffprobe(tmp_video_path)
        fps = meta["fps"]
        frame_step = max(1, int(interval * fps))

        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-i",
                tmp_video_path,
                "-vf",
                f"select='not(mod(n,{frame_step}))'",
                "-vsync",
                "vfr",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        results: list[dict] = []
        frame_num = 0
        buf = b""
        assert proc.stdout is not None

        while len(results) < max_frames:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            end = buf.find(b"\xff\xd9")
            if end == -1:
                continue
            jpg_data = buf[: end + 2]
            buf = buf[end + 2 :]

            img = Image.open(io.BytesIO(jpg_data))
            r = predict_trained_model(
                job.model_path,
                img,
                device=settings.resolved_device,
                conf=conf,
                iou=iou,
            )

            boxes_camel = [
                {
                    "className": b["class_name"],
                    "confidence": b["confidence"],
                    "x1": b["x1"],
                    "y1": b["y1"],
                    "x2": b["x2"],
                    "y2": b["y2"],
                }
                for b in r["boxes"]
            ]
            results.append(
                {
                    "frameNumber": frame_num,
                    "timestampSeconds": round(frame_num / fps * frame_step, 3),
                    "imageWidth": r["image_width"],
                    "imageHeight": r["image_height"],
                    "boxes": boxes_camel,
                }
            )
            frame_num += 1

        proc.terminate()
        return APIResponse(
            data={
                "modelVariant": job.model_variant,
                "frameCount": len(results),
                "frames": results,
            }
        )
    finally:
        Path(tmp_video_path).unlink(missing_ok=True)
        get_memory_manager().full_cleanup()


# ── Annotated video export ────────────────────────────


@router.post("/jobs/{job_id}/export-video/{video_id}")
def export_annotated_video(
    job_id: UUID,
    video_id: UUID,
    db: Session = Depends(get_db),
    conf: float = Query(0.25),
    iou: float = Query(0.45),
    max_frames: int = Query(0, ge=0),
):
    """Render YOLO detections onto every frame and return a downloadable MP4."""
    job = db.query(TrainingJob).filter(TrainingJob.id == job_id).first()
    if not job or not job.model_path:
        raise HTTPException(404, "Trained model not found")
    if job.status != "completed":
        raise HTTPException(400, "Training not completed")
    if not Path(job.model_path).exists():
        raise HTTPException(404, "Model file not found on disk")

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(404, "Video not found")
    video_path = Path(video.file_path)
    if not video_path.exists():
        raise HTTPException(404, "Video file not found on disk")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise HTTPException(400, "Cannot open video file")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise HTTPException(400, "Invalid video dimensions")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as raw_tmp:
        raw_path = Path(raw_tmp.name)
    writer = cv2.VideoWriter(str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    frame_num = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            result = predict_trained_model(
                job.model_path, pil_img, device=settings.resolved_device, conf=conf, iou=iou
            )
            for b in result["boxes"]:
                x1, y1, x2, y2 = b["x1"], b["y1"], b["x2"], b["y2"]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                label = f"{b['class_name']} {b['confidence']:.2f}"
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(frame, (x1, y1 - th - 4), (x1 + tw + 4, y1), (0, 255, 0), -1)
                cv2.putText(
                    frame,
                    label,
                    (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 0, 0),
                    1,
                )
            ts_str = f"Frame {frame_num} | {frame_num / fps:.1f}s"
            cv2.putText(frame, ts_str, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            writer.write(frame)
            frame_num += 1
            if max_frames and frame_num >= max_frames:
                break
    finally:
        cap.release()
        writer.release()
        get_memory_manager().full_cleanup()

    if frame_num == 0:
        raw_path.unlink(missing_ok=True)
        raise HTTPException(400, "No frames were decoded from the video")

    # Transcode to H.264 for broad browser/QuickTime compatibility.
    final_path = raw_path.with_name(raw_path.stem + "_h264.mp4")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                str(final_path),
            ],
            capture_output=True,
            timeout=900,
            check=False,
        )
    except Exception:
        final_path = None
    use_path = (
        final_path
        if final_path and final_path.exists() and final_path.stat().st_size > 0
        else raw_path
    )
    if use_path != raw_path:
        raw_path.unlink(missing_ok=True)
    return FileResponse(
        str(use_path),
        media_type="video/mp4",
        filename=f"annotated_{Path(video.file_name).stem}.mp4",
        background=BackgroundTask(lambda: Path(use_path).unlink(missing_ok=True)),
    )
