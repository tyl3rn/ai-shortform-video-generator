"""
daily.py

The once-a-day job on the Raspberry Pi: make TARGET videos, then upload them
to a OneDrive folder so they can be grabbed on a phone and posted to TikTok
by hand.

  - Runs main.py against a subreddit from the pool. If that crawl yields fewer
    than TARGET videos (nothing cleared the score bar), tries a different
    subreddit, up to MAX_ATTEMPTS crawls.
  - Each finished video lands in demo/ as usual, and is uploaded with
    rclone to OneDrive/showrunner/<date>/ next to a .caption.txt holding the
    TikTok caption, ready to copy-paste. The rclone destination defaults to
    "onedrive:showrunner"; override with SHOWRUNNER_RCLONE_DEST in .env.
  - Cleans up after itself so the SD card and OneDrive don't fill: videos
    older than KEEP_LOCAL_DAYS are deleted from demo/, and day folders older
    than KEEP_ONEDRIVE_DAYS from OneDrive.
  - Everything is logged to run_output/daily.log.

Scheduled by cron (pi/setup.sh installs it); by hand:

    .venv/bin/python daily.py
"""
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DEMO_DIR = ROOT / "demo"
RUN_OUTPUT = ROOT / "run_output"
LOG_FILE = RUN_OUTPUT / "daily.log"
BACKGROUND = ROOT / "backgrounds" / "parkour.mp4"
EXPORT_STAGING = RUN_OUTPUT / "export"

load_dotenv(ROOT / ".env")
RCLONE_DEST = os.environ.get("SHOWRUNNER_RCLONE_DEST", "onedrive:showrunner").rstrip("/")

TARGET = 2
MAX_ATTEMPTS = 3

# Videos run ~70-90 MB each. 30 days on the Pi is ~5 GB of a 32 GB card;
# 14 days on OneDrive is ~2.5 GB, inside the free plan's 5 GB.
KEEP_LOCAL_DAYS = 30
KEEP_ONEDRIVE_DAYS = 14


def log(msg: str):
    RUN_OUTPUT.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def pick_subreddits() -> list:
    """Distinct subreddits from the pool, in a weighted-random order (the
    pool's repeats are its weighting)."""
    sys.path.insert(0, str(ROOT))
    from curate import SUBREDDIT_POOL
    pool, order = list(SUBREDDIT_POOL), []
    while pool:
        sub = random.choice(pool)
        order.append(sub)
        pool = [s for s in pool if s != sub]
    return order


def stage_export(mp4: Path, dest: Path):
    """Video + its TikTok caption as a .txt, side by side in `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mp4, dest / mp4.name)
    upload = mp4.with_suffix(".upload.json")
    if upload.exists():
        copy = json.loads(upload.read_text(encoding="utf-8"))
        (dest / f"{mp4.stem}.caption.txt").write_text(copy.get("tiktok_caption", ""), encoding="utf-8")


def cleanup():
    cutoff = time.time() - KEEP_LOCAL_DAYS * 86400
    removed = 0
    for mp4 in DEMO_DIR.glob("*.mp4"):
        if mp4.stat().st_mtime < cutoff:
            # the video plus its sidecars (captions, scorecard)
            for f in DEMO_DIR.glob(f"{mp4.stem}.*"):
                f.unlink(missing_ok=True)
            removed += 1
    if removed:
        log(f"cleanup: deleted {removed} video(s) older than {KEEP_LOCAL_DAYS} days from demo/")

    age = f"{KEEP_ONEDRIVE_DAYS}d"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            # files first, then the day folders they leave empty
            for cmd in (["rclone", "delete", RCLONE_DEST, "--min-age", age],
                        ["rclone", "rmdirs", RCLONE_DEST, "--leave-root"]):
                code = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
                if code != 0:
                    log(f"cleanup: {' '.join(cmd[:2])} failed (exit {code})")
                    break
    except FileNotFoundError:
        log("cleanup: rclone not installed, skipped OneDrive cleanup")


def main():
    log("=== daily run starting ===")
    made = []
    for attempt, sub in enumerate(pick_subreddits()[:MAX_ATTEMPTS], 1):
        need = TARGET - len(made)
        if need <= 0:
            break
        log(f"attempt {attempt}/{MAX_ATTEMPTS}: r/{sub}, want {need} more")
        before = set(DEMO_DIR.glob("*.mp4")) if DEMO_DIR.exists() else set()
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), sub,
                 "--max-videos", str(need),
                 "--background", str(BACKGROUND),
                 "--workdir", str(RUN_OUTPUT),
                 "--out-dir", str(DEMO_DIR)],
                cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
        new = sorted(set(DEMO_DIR.glob("*.mp4")) - before)
        made += new
        log(f"attempt {attempt}: exit {result.returncode}, {len(new)} new video(s)")

    day = time.strftime("%Y-%m-%d")
    # Stage video + caption pairs locally, upload in one rclone call, and
    # drop the staging copy once it's safely in OneDrive.
    dest = EXPORT_STAGING / day
    for mp4 in made:
        try:
            stage_export(mp4, dest)
        except OSError as e:
            log(f"export failed for {mp4.name}: {e}")
    if made:
        remote = f"{RCLONE_DEST}/{day}"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                code = subprocess.run(["rclone", "copy", str(dest), remote],
                                      stdout=fh, stderr=subprocess.STDOUT).returncode
        except FileNotFoundError:
            code = "rclone not installed"
        if code == 0:
            shutil.rmtree(dest, ignore_errors=True)
            dest = remote
        else:
            log(f"upload to {remote} failed ({code}); files kept in {dest}")
    log(f"=== done: {len(made)}/{TARGET} video(s)"
        + (f", exported to {dest}" if made else "") + " ===")
    cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # cron discards output -- the log is the only place a crash shows up.
        import traceback
        log("daily run crashed:\n" + traceback.format_exc())
        raise
