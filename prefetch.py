"""
prefetch.py

Background crawler that keeps a local cache of reddit listings + top comments
warm, so curate.py can skip the rate-limited crawl (~9 min in RSS mode) and
go straight to Claude scoring.

It runs forever at reddit's pace (~1 request/min without API credentials),
cycling through the subreddit pool and re-crawling whichever subreddit's
listing has gone stale. The web console starts it automatically; on its own:

    python prefetch.py

curate.py reads the cache and falls back to a live crawl for anything missing
or stale. While curate is crawling it pauses the prefetcher (via a pause file)
so the two processes don't trip each other's rate limit.

Files (all in run_output/): prefetch_cache.json (the data),
prefetch_status.json (what the console shows), prefetch.pause, prefetch.lock.
"""
import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

from reddit_fetch import fetch_comments, fetch_listing, using_oauth

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "run_output"
CACHE_FILE = CACHE_DIR / "prefetch_cache.json"
STATUS_FILE = CACHE_DIR / "prefetch_status.json"
PAUSE_FILE = CACHE_DIR / "prefetch.pause"
LOCK_FILE = CACHE_DIR / "prefetch.lock"

# Re-crawl a subreddit once its listing is this old. A full sweep of the
# 9-sub pool takes ~80 min in RSS mode, so 2h keeps the whole pool warm.
REFRESH_AGE = 2 * 3600
# curate.py ignores cached listings older than this and crawls live instead.
LISTING_MAX_AGE = 6 * 3600

# The only post fields curate reads. OAuth listings carry ~100 fields per
# post; storing them all would bloat the cache for nothing.
POST_FIELDS = ("id", "title", "selftext", "permalink", "subreddit", "stickied",
               "score", "upvote_ratio", "num_comments")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data):
    """Atomic write, so a reader never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open
            time.sleep(0.1)
    os.replace(tmp, path)


def _key(subreddit: str, listing: str, time_filter: str) -> str:
    return f"{subreddit.lower()}|{listing}|{time_filter}"


# ---------------------------------------------------------------------------
# Reader side (used by curate.py)
# ---------------------------------------------------------------------------

def cached_listing(subreddit: str, listing: str = "top", time_filter: str = "day",
                   max_age: float = LISTING_MAX_AGE) -> list | None:
    """Cached posts for this listing, or None if missing or too old."""
    entry = _read_json(CACHE_FILE, {}).get("listings", {}).get(_key(subreddit, listing, time_filter))
    if not entry or time.time() - entry["fetched_at"] > max_age:
        return None
    return entry["posts"]


def cached_comments(post_id: str) -> list | None:
    entry = _read_json(CACHE_FILE, {}).get("comments", {}).get(post_id)
    return entry["comments"] if entry else None


@contextmanager
def paused(minutes: float = 30):
    """Keep the prefetcher off reddit while a live crawl runs. The pause
    expires by itself if the holder crashes without cleaning up."""
    CACHE_DIR.mkdir(exist_ok=True)
    PAUSE_FILE.write_text(str(time.time() + minutes * 60), encoding="utf-8")
    try:
        yield
    finally:
        PAUSE_FILE.unlink(missing_ok=True)


def status() -> dict:
    """Snapshot for the web console."""
    st = _read_json(STATUS_FILE, {})
    now = time.time()
    # one RSS request can block ~4 min when 429 retries pile up
    alive = now - st.get("heartbeat", 0) < 360
    ready = sorted(
        sub for sub, s in st.get("subs", {}).items()
        if s.get("complete") and now - s.get("listed_at", 0) < LISTING_MAX_AGE
    )
    return {
        "running": alive,
        "activity": st.get("activity", "") if alive else "",
        "ready": ready,
        "total": len(st.get("subs", {})),
    }


# ---------------------------------------------------------------------------
# Crawler side
# ---------------------------------------------------------------------------

def _paused_until() -> float:
    try:
        return float(PAUSE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0.0


def _acquire_lock():
    """Exclusive lock on LOCK_FILE for the life of the process -- two
    prefetchers would double the request rate and both get 429'd. Returns
    the open handle (keep it referenced), or None if another one holds it."""
    CACHE_DIR.mkdir(exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Prefetcher:
    def __init__(self, args):
        import curate  # deferred: curate imports this module
        self.curate = curate
        self.args = args
        self.subs = list(dict.fromkeys(args.subreddits or curate.SUBREDDIT_POOL))
        self.cache = _read_json(CACHE_FILE, {})
        self.cache.setdefault("listings", {})
        self.cache.setdefault("comments", {})
        self.activity = "starting"

    # -- cache helpers ------------------------------------------------------

    def _listing(self, sub):
        return self.cache["listings"].get(_key(sub, self.args.listing, self.args.time_filter))

    def _age(self, sub) -> float:
        entry = self._listing(sub)
        return time.time() - entry["fetched_at"] if entry else float("inf")

    def _targets(self, sub) -> list:
        """The exact candidates curate.py would fetch comments for."""
        entry = self._listing(sub)
        if not entry:
            return []
        seen_path = Path(self.args.seen_file)
        seen = set(_read_json(seen_path, [])) if seen_path.exists() else set()
        return self.curate.shortlist(entry["posts"], sub, self.args.limit,
                                     self.args.min_len, self.args.max_len, seen)

    def _missing(self, sub) -> list:
        return [c for c in self._targets(sub) if c["id"] not in self.cache["comments"]]

    def _save(self):
        _write_json(CACHE_FILE, self.cache)
        self._write_status()

    def _write_status(self):
        subs = {}
        for sub in self.subs:
            entry = self._listing(sub)
            if entry:
                # "complete" = a run here needs no live requests (and has
                # something to judge -- curate skips empty shortlists too)
                complete = bool(self._targets(sub)) and not self._missing(sub)
                subs[sub] = {"listed_at": entry["fetched_at"], "complete": complete}
            else:
                subs[sub] = {"listed_at": 0, "complete": False}
        _write_json(STATUS_FILE, {"heartbeat": time.time(), "activity": self.activity, "subs": subs})

    def _set_activity(self, text):
        self.activity = text
        self._write_status()

    def _prune(self):
        """Drop comments for posts no longer in any cached listing."""
        live = {p["id"] for e in self.cache["listings"].values() for p in e["posts"]}
        self.cache["comments"] = {k: v for k, v in self.cache["comments"].items() if k in live}

    # -- crawling -----------------------------------------------------------

    def _wait_if_paused(self):
        announced = False
        while (until := _paused_until()) > time.time():
            if not announced:
                log("paused: a generation run is crawling reddit")
                announced = True
            self._set_activity("paused while a generation run crawls")
            time.sleep(max(1.0, min(15.0, until - time.time())))
        if announced:
            log("resumed")

    def _pick(self):
        """Finish a fresh-but-incomplete subreddit first (don't waste its
        listing request), else re-crawl the stalest one past REFRESH_AGE."""
        for sub in self.subs:
            if self._age(sub) < REFRESH_AGE and self._missing(sub):
                return sub
        stalest = max(self.subs, key=self._age)
        return stalest if self._age(stalest) >= REFRESH_AGE else None

    def _crawl(self, sub):
        a = self.args
        if self._age(sub) >= REFRESH_AGE:
            self._wait_if_paused()
            self._set_activity(f"r/{sub}: listing")
            log(f"r/{sub}: fetching {a.listing}/{a.time_filter} listing")
            posts = fetch_listing(sub, a.listing, a.time_filter, a.limit)
            self.cache["listings"][_key(sub, a.listing, a.time_filter)] = {
                "fetched_at": time.time(),
                "posts": [{f: p.get(f) for f in POST_FIELDS} for p in posts],
            }
            self._prune()
            self._save()

        missing = self._missing(sub)
        for i, cand in enumerate(missing, 1):
            self._wait_if_paused()
            if cand["id"] in self.cache["comments"]:
                continue
            self._set_activity(f"r/{sub}: comments {i}/{len(missing)}")
            log(f"r/{sub}: comments {i}/{len(missing)} -- {cand['title'][:60]}")
            comments = fetch_comments(cand["permalink"], limit=self.curate.COMMENTS_PER_POST)
            self.cache["comments"][cand["id"]] = {"fetched_at": time.time(), "comments": comments}
            self._save()
        if self._targets(sub):
            log(f"r/{sub}: ready")
        else:
            log(f"r/{sub}: no usable candidates (all seen or outside the length window)")

    def run(self):
        mode = "OAuth API" if using_oauth() else "RSS (~1 request/min)"
        log(f"prefetching {', '.join('r/' + s for s in self.subs)} via {mode}")
        idle_logged = False
        while True:
            sub = self._pick()
            if sub is None:
                if not idle_logged:
                    log("all subreddits fresh; idling")
                    idle_logged = True
                self._set_activity("idle: everything fresh")
                time.sleep(60)
                continue
            idle_logged = False
            try:
                self._crawl(sub)
            except Exception as e:
                log(f"r/{sub}: {type(e).__name__}: {e} -- backing off 2 min")
                self._set_activity(f"error on r/{sub}, retrying soon")
                time.sleep(120)


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Keep a warm cache of reddit candidates for curate.py.")
    ap.add_argument("subreddits", nargs="*", help="subreddits to cache (default: curate.py's pool)")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "controversial"])
    ap.add_argument("--time-filter", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    # These mirror curate.py's defaults so the cached shortlist matches what
    # a run would pick. Mismatches just mean curate fetches the gaps live.
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--min-len", type=int, default=400)
    ap.add_argument("--max-len", type=int, default=2200)
    ap.add_argument("--seen-file", default=str(CACHE_DIR / "seen_story_ids.json"))
    args = ap.parse_args()

    lock = _acquire_lock()
    if lock is None:
        log("another prefetcher is already running; exiting")
        return
    try:
        Prefetcher(args).run()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        lock.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
