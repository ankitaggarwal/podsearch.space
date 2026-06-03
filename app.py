"""
app.py — the FastAPI web server.

This is the HTTP layer. It owns no retrieval logic of its own; all the real work
lives in the engine (transcribe.py), which this server simply exposes over HTTP and
shares — as the same singleton — with the MCP server.

What it serves:
    - /api/search        the public search endpoint (question in, cited answer out)
    - /api/episodes,     catalog + library + per-episode views for the web UI
      /api/library, …
    - /api/crawler/*     the live indexing crawler: start it, watch its progress
                         stream, and (on production) receive relayed progress events
    - /mcp               the MCP server, mounted so AI agents hit the same engine
    - /                  the single-file React UI (ui/index.html)

Cross-cutting concerns handled here as middleware:
    - Auth        — sensitive endpoints require an X-API-Key (see PROTECTED_PATHS)
    - Rate limit  — a simple in-memory sliding-window limiter, per key or per IP

Two deployment roles, selected by the CRAWLER_ENABLED env var:
    - web  (CRAWLER_ENABLED=false) — just serves search + UI + /mcp
    - pipeline (CRAWLER_ENABLED=true) — also runs the crawler and relays its progress
                                        events to the web instance for the live panel
"""

import logging
import os
import re
import time
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from config import validate_config
from mcp_tools.mcp_server import mcp

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config check ────────────────────────────────────────────────────────
missing = validate_config()
if missing:
    logger.warning("App starting with missing config — some features may fail")

# ── Engine (singleton — shared with MCP server) ────────────────────────
from transcribe import get_engine  # noqa: E402

engine = get_engine()

# ── Auth + Rate Limiting ─────────────────────────────────────────────────
from config import MCP_API_KEYS
_VALID_KEYS = set(k for k in MCP_API_KEYS.split(",") if k)
_rate_lock = threading.Lock()
_rate_buckets: dict = defaultdict(list)   # key -> [timestamps]
RATE_LIMIT = 60                            # max requests per minute per key

PROTECTED_PATHS = ("/mcp", "/api/crawler/start", "/api/crawler/ingest")
CRAWLER_ENABLED = os.getenv("CRAWLER_ENABLED", "true").lower() == "true"
# Public endpoints — rate-limited but no auth required
RATE_LIMITED_PUBLIC = ("/api/search", "/api/episodes", "/api/library", "/api/crawler/status", "/api/crawler/events")
PUBLIC_RATE_LIMIT = 60  # requests per minute per IP


def _client_ip(request) -> str:
    """Extract the real client IP when running behind a reverse proxy.
    Caddy sets `X-Forwarded-For` automatically on every upstream request."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_rate_key(request) -> str:
    api_key = request.headers.get("X-API-Key", "")
    return api_key or _client_ip(request)


_LAST_RATE_GC = 0.0
_RATE_GC_INTERVAL = 300  # sweep stale buckets at most every 5 minutes


def _is_rate_limited(key: str, limit: int = RATE_LIMIT) -> bool:
    """Sliding-window rate limiter with bounded memory.

    Empty buckets and buckets whose last hit is >2× the window are evicted
    every RATE_GC_INTERVAL seconds. This prevents the dict from growing
    unbounded over weeks of unique IPs hitting the server during 24/7 ops.
    """
    global _LAST_RATE_GC
    now = time.time()
    with _rate_lock:
        bucket = [t for t in _rate_buckets[key] if now - t < 60]
        if len(bucket) >= limit:
            _rate_buckets[key] = bucket
            return True
        bucket.append(now)
        _rate_buckets[key] = bucket
        if now - _LAST_RATE_GC > _RATE_GC_INTERVAL:
            _LAST_RATE_GC = now
            stale_cutoff = now - 120
            for k in list(_rate_buckets.keys()):
                ts = _rate_buckets[k]
                if not ts or ts[-1] < stale_cutoff:
                    del _rate_buckets[k]
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Gatekeeper that runs before every request. It classifies the path into one
    of three buckets, in order:
        1. Public + throttled  (search, catalog) — no key, but rate-limited per IP
        2. Protected           (/mcp, crawler control) — must present a valid X-API-Key
        3. Everything else      (static UI, health) — passes straight through
    Keeping all of this in one middleware means the endpoint handlers stay focused
    on their actual job and never repeat auth/throttle logic.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path

        # Reject HEAD on /mcp — Starlette's BaseHTTPMiddleware chokes on the
        # empty-body SSE start response and raises an AssertionError in the ASGI
        # body_stream. Block early to keep logs clean.
        if path.startswith("/mcp") and request.method == "HEAD":
            return JSONResponse({"error": "Method Not Allowed"}, status_code=405)

        # Public rate-limited endpoints (no auth, but throttled per IP)
        if any(path.startswith(p) for p in RATE_LIMITED_PUBLIC):
            ip = _client_ip(request)
            if _is_rate_limited(f"public:{ip}", limit=PUBLIC_RATE_LIMIT):
                return JSONResponse({"error": f"Rate limit exceeded — max {PUBLIC_RATE_LIMIT} requests/min"}, status_code=429)
            return await call_next(request)

        if not any(path.startswith(p) for p in PROTECTED_PATHS):
            return await call_next(request)

        # Auth — always enforce if keys are configured
        api_key = request.headers.get("X-API-Key", "")
        if not _VALID_KEYS or api_key not in _VALID_KEYS:
            return JSONResponse({"error": "Unauthorized — provide a valid X-API-Key header"}, status_code=401)

        # Rate limit
        if _is_rate_limited(_get_rate_key(request)):
            return JSONResponse({"error": f"Rate limit exceeded — max {RATE_LIMIT} requests/min"}, status_code=429)

        return await call_next(request)


# ── App ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Podcast Search Engine", version="1.0.0")
app.add_middleware(AuthMiddleware)
# Mount the MCP server as a sub-application at /mcp. Because it's the SAME engine
# singleton underneath, agents and the web UI query one shared index — "one engine,
# two surfaces." (See the note on mount ordering near the static-file mount below.)
app.mount("/mcp", mcp.sse_app())

# ── Crawler state (thread-safe) ─────────────────────────────────────────
_crawler_lock = threading.Lock()
crawler_events = deque(maxlen=200)
crawler_status = {
    "active": False,
    "processed": 0,
    "current_episode": None,
    "current_phase": None,
    "last_event_ts": 0.0,  # epoch seconds — used to derive "live" on production
}

# Phases the live UI is allowed to show. Anything else (skipped, error,
# internal) is silently dropped to avoid leaking diagnostic noise + give a
# stable, sanitized public surface.
LIVE_ALLOWED_PHASES = frozenset({
    "discovery", "fetching", "transcribing", "embedding",
    "saving", "done", "complete",
})

# An event is considered "live" if seen within this many seconds.
LIVE_ACTIVITY_WINDOW_SECS = 90


def _crawler_log(phase, episode, message):
    with _crawler_lock:
        crawler_events.append({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "phase": phase, "episode": episode, "message": message,
        })
        crawler_status["last_event_ts"] = time.time()
        if phase == "complete":
            crawler_status.update(active=False, current_episode=None, current_phase=None)
        elif phase == "done":
            crawler_status["processed"] += 1
        else:
            if episode:
                crawler_status["current_episode"] = episode
            crawler_status["current_phase"] = phase

    # Forward to production live site (when LIVE_RELAY_URL is set in config).
    # Imported lazily so module import order isn't constrained.
    _live_relay_queue(phase, episode, message)


def _run_crawler():
    with _crawler_lock:
        if crawler_status["active"]:
            return
        crawler_status.update(active=True, processed=0, current_episode=None, current_phase=None)
    try:
        engine.auto_process_episodes(status_callback=_crawler_log, max_episodes=0)
    except Exception as e:
        logger.error("Crawler failed: %s", e)
        _crawler_log("error", "", str(e))
        with _crawler_lock:
            crawler_status["active"] = False


def _warm_then_crawl():
    engine.get_indexed_episodes()   # loads catalog from PostgreSQL on startup
    # Continuous crawler: walk all RSS feeds, process new episodes, sleep, repeat.
    # _run_crawler() resets crawler_status["active"] via the "complete" callback
    # at the end of auto_process_episodes (and via its own except block on
    # failure), so consecutive calls work cleanly. The outer try/except keeps
    # the thread alive across unexpected errors (e.g. transient PG outage).
    while True:
        try:
            _run_crawler()
        except Exception as e:
            logger.error("Crawler iteration failed: %s", e)
        time.sleep(900)  # 15 min between full RSS-feed sweeps


@app.on_event("startup")
def _start_crawler():
    if CRAWLER_ENABLED:
        threading.Thread(target=_warm_then_crawl, daemon=True).start()
        _live_relay_init()  # ← forwards local crawler events to production
    else:
        # Production: warm the cache only — crawler runs separately
        threading.Thread(target=engine.get_indexed_episodes, daemon=True).start()
        logger.info("Crawler disabled (CRAWLER_ENABLED=false) — search-only mode")


# ── Health ──────────────────────────────────────────────────────────────

@app.get("/api/live")
def liveness():
    """Liveness probe — the process is up and answering HTTP. Always 200.
    A transient dependency outage (Pinecone, Postgres) does not fail this check.
    The app is RUNNING; whether it's fully READY is a separate question (/api/health)."""
    return {"status": "live"}


@app.get("/api/health")
def health():
    """Readiness/health probe. Returns 503 (not 200) when any backing service
    is down so monitoring + load-balancers can pull the machine out of rotation
    rather than serving silent 500s. Error strings are deliberately omitted —
    they can carry host fragments in psycopg2's exceptions."""
    checks = {"api": "ok"}
    try:
        engine._pc_vector_count()
        checks["pinecone"] = "ok"
    except Exception:
        checks["pinecone"] = "error"
    try:
        from config import EPISODES_TABLE
        with engine._pg_cur() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {EPISODES_TABLE}")
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "error"
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    body = {"status": overall, "checks": checks}
    return body if overall == "ok" else JSONResponse(body, status_code=503)


# ── Crawler endpoints ───────────────────────────────────────────────────

@app.get("/api/crawler/status")
def get_crawler_status():
    with _crawler_lock:
        # `live` = activity seen within the last LIVE_ACTIVITY_WINDOW_SECS.
        # The UI shows the panel iff the local instance crawls (CRAWLER_ENABLED)
        # OR a remote source is currently relaying events.
        is_live = (time.time() - crawler_status["last_event_ts"]) < LIVE_ACTIVITY_WINDOW_SECS
        return {
            "active": bool(crawler_status["active"] or is_live),
            "processed": crawler_status["processed"],
            "current_episode": crawler_status["current_episode"],
            "current_phase": crawler_status["current_phase"],
            "enabled": bool(CRAWLER_ENABLED or is_live),  # ← show panel if anyone's live
        }


@app.get("/api/crawler/events")
def get_crawler_events():
    # Filter to public-safe phases — never leak internal diagnostics (errors,
    # skipped reasons, internal labels) to anonymous viewers of the live site.
    with _crawler_lock:
        events = [e for e in crawler_events if e.get("phase") in LIVE_ALLOWED_PHASES]
    return {"events": events}


@app.post("/api/crawler/start")
def start_crawler():
    if not CRAWLER_ENABLED:
        return {"status": "disabled", "message": "Crawler runs locally — run python3 transcribe.py --all"}
    with _crawler_lock:
        if crawler_status["active"]:
            return {"status": "already_running", "processed": crawler_status["processed"]}
    engine.reset_cache()
    threading.Thread(target=_run_crawler, daemon=True).start()
    return {"status": "started"}


# ── Live event relay ────────────────────────────────────────────────────
# A LOCAL crawler (running on a Mac with the local LLM stack) POSTs phase
# events here so the production site can show transcription activity in real
# time. The production instance NEVER crawls itself; this endpoint is the
# only way crawler_events gets populated on production. Auth + rate-limited
# upstream by the AuthMiddleware ("/api/crawler/ingest" is in PROTECTED_PATHS).

class IngestEvent(BaseModel):
    phase: str = Field(..., min_length=1, max_length=20)
    episode: str = Field("", max_length=200)
    message: str = Field("", max_length=500)


class IngestPayload(BaseModel):
    # Source identifier — sanitized & length-capped server-side. Don't trust
    # client-supplied hostnames blindly.
    source: str = Field("anonymous", max_length=64)
    events: list[IngestEvent] = Field(..., min_length=1, max_length=50)


_SAFE_SOURCE_RE = re.compile(r"[^a-zA-Z0-9-]")


@app.post("/api/crawler/ingest")
def ingest_crawler_events(payload: IngestPayload):
    """Accept a batch of crawler events from a remote local instance.

    SECURITY:
      - PROTECTED_PATHS in AuthMiddleware enforces X-API-Key BEFORE this runs.
      - Rate limit: 60 requests/min/key (existing middleware limit).
      - Pydantic length caps prevent payload-size abuse (max 50 events,
        each capped at 700 chars). Total max body ~40 KB.
      - Phase whitelist (LIVE_ALLOWED_PHASES) drops anything else silently —
        no way to leak diagnostic phase names through this surface.
      - Source field is sanitized server-side, never echoed in public events.
    """
    accepted = 0
    # Sanitize source: strip anything non-alphanumeric/hyphen, cap to 32 chars
    src = (_SAFE_SOURCE_RE.sub("", payload.source) or "anonymous")[:32]
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with _crawler_lock:
        for ev in payload.events:
            if ev.phase not in LIVE_ALLOWED_PHASES:
                continue   # silently drop non-public phases
            crawler_events.append({
                "timestamp": now_iso,
                "phase": ev.phase,
                "episode": ev.episode,
                "message": ev.message,
                # Note: `source` is intentionally NOT included — see /events filter.
            })
            if ev.phase == "complete":
                crawler_status.update(active=False, current_episode=None, current_phase=None)
            elif ev.phase == "done":
                crawler_status["processed"] += 1
            else:
                if ev.episode:
                    crawler_status["current_episode"] = ev.episode
                crawler_status["current_phase"] = ev.phase
            accepted += 1
        crawler_status["last_event_ts"] = time.time()

    logger.info("Ingested %d/%d events from source=%s", accepted, len(payload.events), src)
    return {"accepted": accepted, "received": len(payload.events)}


# ── Outbound live relay (LOCAL → PRODUCTION) ───────────────────────────
# When CRAWLER_ENABLED is true (this is a local instance) AND LIVE_RELAY_URL
# is configured, every crawler event is also forwarded to the production
# /api/crawler/ingest. Background thread batches up to 1s of events into a
# single POST. Fire-and-forget — failure drops events silently.

_relay_buffer: list = []
_relay_lock = threading.Lock()
_relay_thread_started = False


def _live_relay_queue(phase: str, episode: str, message: str):
    """Append an event to the relay buffer if relay is configured."""
    if not _RELAY_ENABLED or phase not in LIVE_ALLOWED_PHASES:
        return
    with _relay_lock:
        if len(_relay_buffer) >= 200:
            _relay_buffer.pop(0)  # drop oldest if buffer overflows (rare)
        _relay_buffer.append({
            "phase": phase,
            "episode": (episode or "")[:200],
            "message": (message or "")[:500],
        })


def _live_relay_loop():
    """Flush the relay buffer every 1s. Fire-and-forget POST."""
    import urllib.request, urllib.error, json as _json
    url = _RELAY_URL.rstrip("/") + "/api/crawler/ingest"
    headers = {"Content-Type": "application/json", "X-API-Key": _RELAY_AUTH}
    while True:
        time.sleep(1.0)
        with _relay_lock:
            if not _relay_buffer:
                continue
            batch = _relay_buffer[:]
            _relay_buffer.clear()
        try:
            body = _json.dumps({"source": _RELAY_SOURCE, "events": batch}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, Exception) as e:
            # Ephemeral by design — log at debug level, don't retry.
            logger.debug("Relay POST failed (%d events dropped): %s", len(batch), e)


def _live_relay_init():
    """Start the relay thread if conditions are met (idempotent).

    Root-cause guard: if LIVE_RELAY_URL resolves to THIS host (i.e. production
    is also the crawler, common when CRAWLER_ENABLED=true on prod), the relay
    would POST every event back to /api/crawler/ingest which would duplicate
    the event in crawler_events deque. Detect and skip in that case — events
    already land locally via the in-process _crawler_log path.
    """
    global _RELAY_ENABLED, _RELAY_URL, _RELAY_AUTH, _RELAY_SOURCE, _relay_thread_started
    from config import LIVE_RELAY_URL
    import socket
    _RELAY_URL = (LIVE_RELAY_URL or "").strip()
    _RELAY_AUTH = (MCP_API_KEYS.split(",")[0] if MCP_API_KEYS else "").strip()
    _RELAY_SOURCE = re.sub(r"[^a-zA-Z0-9-]", "", socket.gethostname().split(".")[0])[:32] or "local"

    # Skip relay if we'd be POSTing to ourselves (self-loop = duplicate events).
    # Heuristic: probe LIVE_RELAY_URL's /api/live; if the server replies AND we
    # can see the same request hit our local logs, it's us. Cheaper proxy:
    # check if LIVE_RELAY_URL host resolves to a local-running container by
    # comparing to our public-facing hostname env (fallback: assume self-loop
    # when CRAWLER_ENABLED is true AND LIVE_RELAY_URL is set — production
    # crawls and serves the same site).
    is_self_loop = bool(CRAWLER_ENABLED and _RELAY_URL)
    _RELAY_ENABLED = bool(CRAWLER_ENABLED and _RELAY_URL and _RELAY_AUTH and not is_self_loop)

    if is_self_loop:
        logger.info("Live relay disabled — production is the crawler (LIVE_RELAY_URL would self-loop)")
    if _RELAY_ENABLED and not _relay_thread_started:
        threading.Thread(target=_live_relay_loop, daemon=True, name="LiveRelay").start()
        _relay_thread_started = True
        logger.info("Live relay started: source=%s → %s", _RELAY_SOURCE, _RELAY_URL)


# Defaults so module imports don't fail before init
_RELAY_ENABLED = False
_RELAY_URL = ""
_RELAY_AUTH = ""
_RELAY_SOURCE = "local"


# ── Search ──────────────────────────────────────────────────────────────

class SearchQuery(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    episode_id: Optional[str] = None


@app.post("/api/search")
def search(query: SearchQuery):
    result = engine.search_and_answer(query.question, episode_id=query.episode_id)
    return result


# ── Episodes & Library ──────────────────────────────────────────────────

@app.get("/api/episodes/count")
def episodes_count():
    eps = engine.get_indexed_episodes()
    return {"count": len(eps)}


@app.get("/api/episodes")
def episodes():
    eps = engine.get_indexed_episodes()
    return {
        "count": len(eps),
        "episodes": [
            {"id": ep_id, "title": ep["title"], "podcast": ep["podcast_title"]}
            for ep_id, ep in eps.items()
        ],
    }


@app.get("/api/library")
def library():
    return engine.get_library_data()


@app.get("/api/episodes/{episode_id}")
def episode_detail(episode_id: str):
    episodes = engine.get_indexed_episodes()
    ep = episodes.get(episode_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    return {
        "episode_id": episode_id,
        "title": ep["title"],
        "podcast_title": ep["podcast_title"],
        "image_url": ep.get("image_url", ""),
        "audio_url": ep.get("audio_url", ""),   # link to the original episode
    }


@app.get("/api/episodes/{episode_id}/summary")
def episode_summary(episode_id: str):
    """A short, grounded overview for the episode page. Best-effort: if the LLM
    gateway is unavailable, return an empty summary so the UI degrades gracefully
    (it falls back to 'listen to the original')."""
    try:
        return {"summary": engine.summarize_episode(episode_id)}
    except Exception as e:
        logger.warning("summary failed for %s: %s", episode_id, e)
        return {"summary": ""}


# NOTE: there is deliberately no public full-transcript endpoint. The web surface
# exposes only the AI summary + short, cited search excerpts (with links back to the
# original episode), so the site can't be used to scrape whole transcripts. Full
# transcripts remain available only to authenticated agents via the token-gated MCP
# server, which is meant for trusted use.


@app.get("/api/stats")
def stats():
    """Library + pipeline stats for the Pipeline page. Reads Postgres + Pinecone only,
    so it works even when the transcription gateway is down."""
    from config import EPISODES_TABLE, DEFAULT_PODCAST_URLS
    out = {
        "feeds": len(DEFAULT_PODCAST_URLS),
        "episodes": 0, "chunks": 0, "shows": 0, "vectors": 0, "hours": 0.0,
    }
    try:
        with engine._pg_cur() as cur:
            cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(chunk_count),0), COUNT(DISTINCT podcast_title) "
                f"FROM {EPISODES_TABLE}")
            row = cur.fetchone()
            out["episodes"], out["chunks"], out["shows"] = int(row[0]), int(row[1]), int(row[2])
    except Exception as e:
        logger.warning("stats pg query failed: %s", e)
    try:
        out["vectors"] = engine._pc_vector_count()
    except Exception as e:
        logger.warning("stats pinecone query failed: %s", e)
    # ~1,000-char chunks ≈ a couple of spoken minutes each; a friendly rough total.
    out["hours"] = round(out["chunks"] * 2.0 / 60.0, 1)
    return out


# ── Static files ────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("ui/index.html")


app.mount("/ui", StaticFiles(directory="ui"), name="ui")

# Oversize-audio handoff: when an episode is too big for the gateway's 50 MB
# download cap, the indexer compresses it into smaller chunks and the gateway
# fetches them back over public HTTPS. Serve that scratch dir so the gateway can
# reach the chunks at LIVE_AUDIO_BASE_URL. Only mounted when the feature is in
# use (LIVE_AUDIO_BASE_URL set), so it isn't exposed otherwise.
from config import TEMP_AUDIO_DIR, LIVE_AUDIO_BASE_URL  # noqa: E402
if LIVE_AUDIO_BASE_URL:
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    app.mount("/_tmp_audio", StaticFiles(directory=TEMP_AUDIO_DIR), name="tmp_audio")

# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
