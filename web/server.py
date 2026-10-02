"""
web/server.py

Local console for the pipeline: start generation runs, watch progress,
browse finished videos, copy upload captions, and rate videos (ratings feed
the curation judge's taste profile via feedback.py).

    python -m uvicorn web.server:app --port 8000
    -> http://127.0.0.1:8000

State lives on disk (demo/, ratings.json, run_output/web_job.log), so
nothing is lost when the server or browser restarts.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
import feedback  # noqa: E402
import metrics  # noqa: E402

DEMO_DIR = ROOT / "demo"
RUN_OUTPUT = ROOT / "run_output"
LOG_FILE = RUN_OUTPUT / "web_job.log"
BACKGROUND = ROOT / "backgrounds" / "parkour.mp4"
INDEX_HTML = Path(__file__).parent / "index.html"

TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,30}$")

# Stage markers scanned from the end of the log; first match wins.
STAGE_MARKERS = [
    ("Done ->", "finished"),
    ("skipping", "skipped: nothing cleared the score bar"),
    ("Traceback", "failed"),
    ("=== rendering", "rendering videos"),
    ("Writing platform upload copy", "writing upload captions"),
    ("Running the script doctor", "script doctor rewriting"),
    ("Scoring with Claude", "judging candidates"),
    ("Fetching top comments", "fetching comments (rate limited, the slow part)"),
    ("Fetching candidates", "crawling reddit"),
]

app = FastAPI(title="showrunner console")
_job: dict = {"proc": None, "params": None, "started": None}


class GenerateRequest(BaseModel):
    subreddit: str = "auto"
    time_filter: str = "day"
    max_videos: int = 3
    min_score: int = 65


class RateRequest(BaseModel):
    video: str
    rating: int
    note: str = ""


class MetricsRequest(BaseModel):
    video: str
    views: int
    likes: int = 0
    comments: int = 0
    completion_pct: float | None = None
    platform: str = "tiktok"


def _job_running() -> bool:
    return _job["proc"] is not None and _job["proc"].poll() is None


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.read_text(encoding="utf-8")


@app.post("/api/generate")
def generate(req: GenerateRequest):
    if _job_running():
        raise HTTPException(409, "a run is already in progress")
    if req.subreddit != "auto" and not SUBREDDIT_RE.match(req.subreddit):
        raise HTTPException(400, "invalid subreddit name")
    if req.time_filter not in TIME_FILTERS:
        raise HTTPException(400, "invalid time filter")
    if not BACKGROUND.exists():
        raise HTTPException(500, f"background footage missing: {BACKGROUND}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set in the server's environment")

    RUN_OUTPUT.mkdir(exist_ok=True)
    cmd = [
        sys.executable, str(ROOT / "main.py"), req.subreddit,
        "--time-filter", req.time_filter,
        "--max-videos", str(max(1, min(req.max_videos, 5))),
        "--min-score", str(max(0, min(req.min_score, 100))),
        "--background", str(BACKGROUND),
        "--workdir", str(RUN_OUTPUT),
        "--out-dir", str(DEMO_DIR),
    ]
    log_handle = open(LOG_FILE, "wb")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    _job["proc"] = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT,
                                    cwd=ROOT, env=env)
    _job["params"] = req.model_dump()
    _job["started"] = time.time()
    return {"started": True}


@app.get("/api/job")
def job_status():
    log = ""
    if LOG_FILE.exists():
        raw = LOG_FILE.read_bytes()
        log = raw[-6000:].decode("utf-8", errors="replace")

    stage = "idle"
    if log:
        stage = "starting"
        best = -1
        for marker, label in STAGE_MARKERS:
            pos = log.rfind(marker)
            if pos > best:
                best, stage = pos, label

    running = _job_running()
    exit_code = None
    if _job["proc"] is not None and not running:
        exit_code = _job["proc"].returncode
    return {
        "running": running,
        "stage": stage if (running or log) else "idle",
        "exit_code": exit_code,
        "params": _job["params"],
        "elapsed": round(time.time() - _job["started"]) if _job["started"] and running else None,
        "log": log,
    }


@app.get("/api/videos")
def list_videos():
    if not DEMO_DIR.exists():
        return []
    items = []
    for mp4 in sorted(DEMO_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta, upload = {}, {}
        meta_path = mp4.with_suffix(".meta.json")
        upload_path = mp4.with_suffix(".upload.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if upload_path.exists():
            upload = json.loads(upload_path.read_text(encoding="utf-8"))
        rating = feedback.get_rating(mp4.name) or {}
        perf = metrics.get(mp4.name) or {}
        curation = meta.get("curation", {})
        items.append({
            "metrics": {k: perf.get(k) for k in ("views", "likes", "comments", "completion_pct")},
            "file": mp4.name,
            "size_mb": round(mp4.stat().st_size / 1e6, 1),
            "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(mp4.stat().st_mtime)),
            "title": meta.get("title") or mp4.stem,
            "subreddit": meta.get("subreddit", ""),
            "judged_score": curation.get("overall"),
            "category": curation.get("category", ""),
            "ending_rewritten": meta.get("ending_rewritten"),
            "tiktok_caption": upload.get("tiktok_caption", ""),
            "youtube_title": upload.get("youtube_title", ""),
            "rating": rating.get("rating"),
            "note": rating.get("note", ""),
        })
    return items


@app.get("/api/video/{name}")
def serve_video(name: str):
    if Path(name).name != name or not name.endswith(".mp4"):
        raise HTTPException(400, "bad name")
    path = DEMO_DIR / name
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="video/mp4")


def _video_meta(video: str) -> dict:
    """Meta sidecar if present, else reconstructed from upload copy/filename
    (older videos predate the sidecar)."""
    meta_path = (DEMO_DIR / video).with_suffix(".meta.json")
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    stem = Path(video).stem
    meta = {"title": stem, "subreddit": stem.split("-")[0], "curation": {}}
    upload_path = (DEMO_DIR / video).with_suffix(".upload.json")
    if upload_path.exists():
        upload = json.loads(upload_path.read_text(encoding="utf-8"))
        if upload.get("youtube_title"):
            meta["title"] = upload["youtube_title"]
    return meta


@app.post("/api/rate")
def rate(req: RateRequest):
    if not (1 <= req.rating <= 5):
        raise HTTPException(400, "rating must be 1-5")
    if Path(req.video).name != req.video or not (DEMO_DIR / req.video).exists():
        raise HTTPException(404, "video not found")
    feedback.record_rating(req.video, req.rating, req.note, _video_meta(req.video))
    return {"saved": True}


@app.post("/api/metrics")
def save_metrics(req: MetricsRequest):
    if Path(req.video).name != req.video or not (DEMO_DIR / req.video).exists():
        raise HTTPException(404, "video not found")
    if req.views < 0 or req.likes < 0 or req.comments < 0:
        raise HTTPException(400, "counts cannot be negative")
    if req.completion_pct is not None and not (0 <= req.completion_pct <= 100):
        raise HTTPException(400, "completion must be 0-100")
    metrics.record(req.video, req.views, req.likes, req.comments,
                   req.completion_pct, req.platform, _video_meta(req.video))
    return {"saved": True}


@app.get("/api/analysis")
def get_analysis():
    return metrics.analysis()
