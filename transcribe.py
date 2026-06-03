"""
transcribe.py — the core retrieval engine.

This single module is the whole pipeline. If you read one file to understand the
project, read this one. It does four jobs, in order:

    1. INGEST    — read podcast RSS feeds, resolve each episode's real audio URL,
                   and decide which episodes are worth processing (dedup + ordering).
    2. TRANSCRIBE— send audio to a self-hosted, OpenAI-compatible inference gateway
                   (Parakeet) and get back text with per-utterance timestamps.
    3. INDEX     — split the transcript into ~1,000-char chunks and upsert each as a
                   text record to Pinecone, whose integrated embedding model vectorizes
                   it. The raw transcript is also saved to PostgreSQL as a safety net.
    4. SEARCH    — given a question, Pinecone embeds it and returns the nearest chunks;
                   we re-rank them (hybrid: semantic score + keyword overlap) and ask
                   the LLM to write an answer using ONLY those chunks.

Design principle — "grounded, not generative": the LLM is treated as a synthesizer,
not a knowledge base. Every claim in an answer must trace back to a retrieved excerpt,
and the timestamps ride along with every chunk so the answer can cite the exact moment.

Two data stores, distinct roles:
    - PostgreSQL holds the authoritative catalog + raw transcripts (the facts).
    - Pinecone holds the chunk vectors and does the embedding (integrated model).

The engine is exposed as a process-wide singleton via get_engine() (bottom of file),
so the web app (app.py) and the MCP server (mcp_tools/mcp_server.py) share one set of
clients and caches.
"""

import os
import hashlib
import json
import subprocess
import time as _time
import logging
import threading
import uuid as _uuid
import feedparser
import requests
from itertools import zip_longest
from typing import List, Dict, Optional, Tuple
import re
from openai import OpenAI
from config import (
    PINECONE_API_KEY, PINECONE_HOST, PINECONE_NAMESPACE, PINECONE_API_VERSION, PINECONE_TEXT_FIELD,
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
    TEMP_AUDIO_DIR, LIVE_AUDIO_BASE_URL, OVERSIZE_BYTES,
    DEFAULT_PODCAST_URLS, SYSTEM_PROMPT, build_user_prompt, DATABASE_URL,
    EPISODES_TABLE, TRANSCRIPTS_TABLE,
    ANSWER_LLM_BASE_URL, ANSWER_LLM_API_KEY, ANSWER_LLM_MODEL,
)

logger = logging.getLogger(__name__)

AUDIO_CACHE_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_cache")
CATALOG_TTL_SECS   = 120   # Re-sync episode catalog from PostgreSQL every 2 minutes
POLL_INTERVAL = 10    # seconds between transcription job status polls
JOB_TIMEOUT   = 3600  # max seconds to wait for a transcription job
# A self-hosted gateway loads models on demand, so the FIRST submit after an idle
# period (or a restart) can take well over a minute while the model warms up.
# Keep this generous — a premature timeout just wastes the queued job.
SUBMIT_TIMEOUT = 180  # seconds to wait for the /v1/transcribe-url submit to return 202


class PodcastEngine:
    """Core engine: manages transcription, vector search (Pinecone), and the catalog.

    Embeddings are NOT computed here — Pinecone's integrated embedding model embeds
    both the upserted chunk text and the search query, so this engine only sends
    text. The local gateway (LLM_BASE_URL) is used solely for transcription. Answer
    synthesis runs on answer_client (a fast cloud endpoint, e.g. Google AI Studio)."""

    def __init__(self):
        # Answer-synthesis client: a fast OpenAI-compatible endpoint if configured
        # (e.g. Google AI Studio), else the local gateway. Bounded timeout/retries so
        # a slow upstream can't make a search hang.
        if ANSWER_LLM_BASE_URL:
            self.answer_client = OpenAI(api_key=ANSWER_LLM_API_KEY, base_url=ANSWER_LLM_BASE_URL,
                                        timeout=30.0, max_retries=1)
            self.answer_model = ANSWER_LLM_MODEL
            logger.info("Answer synthesis on external endpoint: model=%s", ANSWER_LLM_MODEL)
        else:
            self.answer_client = OpenAI(api_key=LLM_API_KEY, base_url=f"{LLM_BASE_URL}/v1",
                                        timeout=30.0, max_retries=1)
            self.answer_model = LLM_MODEL

        # Pinecone (integrated embedding) — REST, so no heavy SDK. One session with the
        # records API base for this index.
        self._pc = requests.Session()
        self._pc.headers.update({"Api-Key": PINECONE_API_KEY,
                                 "X-Pinecone-API-Version": PINECONE_API_VERSION})
        self._pc_base = f"{PINECONE_HOST.rstrip('/')}/records/namespaces/{PINECONE_NAMESPACE}"
        self._pc_host = PINECONE_HOST.rstrip('/')

        self._http = requests.Session()
        self._http.headers.update({"User-Agent": "PodcastSearchEngine/1.0"})

        # Episode catalog cache (in-memory, PostgreSQL-backed, TTL-refreshed)
        self._episodes_cache: Optional[Dict] = None
        self._episodes_cache_time: float = 0
        self._episodes_lock = threading.Lock()

        # Library grouping cache
        self._library_cache: Optional[Dict] = None
        self._library_cache_time: float = 0

        # audio_cache/ is used as a scratch dir for oversize-audio downloads
        # (only when ffmpeg pre-compression is needed). Orphans from prior runs
        # are cleared on startup.
        os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
        try:
            for f in os.listdir(AUDIO_CACHE_DIR):
                if f.endswith((".mp3", ".m4a", ".wav", ".ogg", ".flac", ".webm")):
                    os.remove(os.path.join(AUDIO_CACHE_DIR, f))
        except Exception as e:
            logger.warning("Audio cache cleanup failed: %s", e)
        # Clean orphan compressed audio from previous failed runs (Caddy serves this dir)
        try:
            os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
            for f in os.listdir(TEMP_AUDIO_DIR):
                if f.endswith(".webm"):
                    os.remove(os.path.join(TEMP_AUDIO_DIR, f))
        except (PermissionError, OSError):
            pass  # not on server, fine

        # PostgreSQL connection pool — one connection per thread.
        # psycopg2 connections are NOT thread-safe; sharing a single conn between
        # the crawler thread, FastAPI request threads, and MCP tool calls causes
        # protocol-state corruption. ThreadedConnectionPool gives each caller its
        # own connection on demand, returns it to the pool on release.
        self._pg_pool = self._pg_pool_create()

        # Load catalog from PostgreSQL on startup
        self._load_catalog_from_pg()

    # ── PostgreSQL (podcast_ tables) ────────────────────────────────────

    def _pg_pool_create(self):
        """Create the thread-safe connection pool. Pool sizing: 2-10 conns is
        enough for one crawler + a few API/MCP threads."""
        from psycopg2.pool import ThreadedConnectionPool
        return ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)

    def _pg_cur(self):
        """Acquire a connection from the pool and return its cursor as a context
        manager that releases the connection back to the pool on exit.

        Usage (must always use `with` to ensure release):
            with self._pg_cur() as cur:
                cur.execute(...)
        """
        from contextlib import contextmanager

        @contextmanager
        def _cursor_ctx():
            conn = self._pg_pool.getconn()
            try:
                conn.autocommit = True
                cur = conn.cursor()
                try:
                    yield cur
                finally:
                    cur.close()
            finally:
                self._pg_pool.putconn(conn)

        return _cursor_ctx()

    def _load_catalog_from_pg(self):
        """Load episode catalog from PostgreSQL. Called on startup and on TTL expiry."""
        try:
            with self._pg_cur() as cur:
                cur.execute(f"SELECT id, title, podcast_title, image_url, audio_url FROM {EPISODES_TABLE}")
                rows = cur.fetchall()
            self._episodes_cache = {
                row[0]: {"title": row[1], "podcast_title": row[2],
                         "image_url": row[3] or "", "audio_url": row[4] or ""}
                for row in rows
            }
            self._episodes_cache_time = _time.time()
            logger.info("Loaded %d episodes from PostgreSQL", len(self._episodes_cache))
        except Exception as e:
            logger.warning("Could not load catalog from PostgreSQL: %s", e)
            if self._episodes_cache is None:
                self._episodes_cache = {}

    def _save_episode_to_pg(self, episode_id: str, title: str, podcast_title: str,
                             audio_url: str, image_url: str, chunk_count: int):
        """Insert or update episode record in podcast_episodes."""
        try:
            with self._pg_cur() as cur:
                cur.execute(f"""
                    INSERT INTO {EPISODES_TABLE} (id, title, podcast_title, audio_url, image_url, chunk_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        audio_url   = COALESCE(NULLIF(EXCLUDED.audio_url, ''), {EPISODES_TABLE}.audio_url),
                        chunk_count = EXCLUDED.chunk_count,
                        indexed_at  = NOW()
                """, (episode_id, title, podcast_title, audio_url, image_url, chunk_count))
        except Exception as e:
            logger.error("PG episode upsert failed: %s", e)


    # ── Identity ────────────────────────────────────────────────────────
    #
    # Both IDs are *deterministic* — derived purely from the content, never
    # random. That makes the whole pipeline idempotent: re-processing the same
    # episode overwrites its existing rows/points instead of creating duplicates,
    # so a re-run is safe and a crash mid-run is recoverable.

    @staticmethod
    def _episode_id(podcast_title: str, episode_title: str) -> str:
        # Stable primary key for an episode: a hash of show + title. (An audio-URL
        # hash would also work; title-based keeps it readable in the DB.)
        return hashlib.md5(f"{podcast_title}_{episode_title}".encode()).hexdigest()

    @staticmethod
    def _chunk_id(episode_id: str, chunk_index: int) -> str:
        """Stable record id `<episode_id>_c<NNNN>` — same input → same id, so
        re-indexing a chunk overwrites it cleanly. The `_c<NNNN>` suffix also lets
        us list a single episode's chunks by id prefix."""
        return f"{episode_id}_c{chunk_index:04d}"

    # ── Pinecone (integrated embedding) ─────────────────────────────────
    # Pinecone embeds the PINECONE_TEXT_FIELD on upsert and embeds the query on
    # search, so we only ever send/receive text — no vectors cross the wire.

    def _pc_upsert(self, records: List[Dict]) -> None:
        """Upsert text records (NDJSON). Each record: {_id, text, ...metadata}.
        Pinecone embeds the text field with the index's integrated model."""
        if not records:
            return
        for i in range(0, len(records), 96):  # keep request bodies modest
            batch = records[i:i + 96]
            body = "\n".join(json.dumps(r) for r in batch)
            r = self._pc.post(f"{self._pc_base}/upsert",
                              data=body.encode("utf-8"),
                              headers={"Content-Type": "application/x-ndjson"}, timeout=60)
            r.raise_for_status()

    def _pc_search(self, text: str, top_k: int = 25, episode_id: str = None) -> List[Dict]:
        """Search by text (Pinecone embeds the query). Returns normalized matches:
        {id, score, text, episode_id, episode_title, podcast_title, start_time, end_time, chunk_index}."""
        query = {"inputs": {"text": text}, "top_k": top_k}
        if episode_id:
            query["filter"] = {"episode_id": {"$eq": episode_id}}
        fields = ["text", "episode_id", "episode_title", "podcast_title",
                  "chunk_index", "start_time", "end_time"]
        r = self._pc.post(f"{self._pc_base}/search",
                         json={"query": query, "fields": fields},
                         headers={"Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        hits = (r.json().get("result") or {}).get("hits") or []
        out = []
        for h in hits:
            f = h.get("fields") or {}
            out.append({
                "id": h.get("_id"), "score": float(h.get("_score") or 0),
                "text": f.get("text", ""),
                "episode_id": f.get("episode_id", ""),
                "episode_title": f.get("episode_title", "Unknown"),
                "podcast_title": f.get("podcast_title", "Unknown"),
                "chunk_index": f.get("chunk_index"),
                "start_time": f.get("start_time"), "end_time": f.get("end_time"),
            })
        return out

    def _pc_vector_count(self) -> int:
        """Total vectors in the index (for stats/health)."""
        r = self._pc.post(f"{self._pc_host}/describe_index_stats", json={},
                         headers={"Content-Type": "application/json"}, timeout=15)
        r.raise_for_status()
        return int(r.json().get("totalVectorCount") or 0)

    # ── Episode Catalog (PostgreSQL-backed) ─────────────────────────────

    def episode_exists(self, episode_id: str) -> bool:
        """Check if episode is indexed (PostgreSQL is source of truth)."""
        try:
            with self._pg_cur() as cur:
                cur.execute(f"SELECT 1 FROM {EPISODES_TABLE} WHERE id = %s", (episode_id,))
                return cur.fetchone() is not None
        except Exception as e:
            logger.warning("PG episode check failed for %s: %s", episode_id, e)
            return False

    def get_indexed_episodes(self) -> Dict:
        """Return episode catalog — served from cache, refreshed from PostgreSQL every 2 min.

        Stale-while-revalidate: if cache is fresh, return instantly. If stale (TTL expired),
        reload from PG. This ensures locally-indexed episodes appear on the live server
        within CATALOG_TTL_SECS without a server restart.

        Returns a shallow copy so callers can iterate safely even if the crawler
        thread is upserting into the underlying cache concurrently.
        """
        now = _time.time()
        with self._episodes_lock:
            if self._episodes_cache is None or now - self._episodes_cache_time >= CATALOG_TTL_SECS:
                self._load_catalog_from_pg()
            return dict(self._episodes_cache or {})

    def reset_cache(self):
        """Clear in-memory caches so next access re-fetches from PostgreSQL."""
        with self._episodes_lock:
            self._episodes_cache = None
        self._library_cache = None
        self._library_cache_time = 0

    def _upsert_episode_catalog(self, episode_id: str, episode_title: str,
                                podcast_title: str, audio_url: str = "",
                                image_url: str = "", chunk_count: int = 0):
        # PostgreSQL — source of truth
        self._save_episode_to_pg(episode_id, episode_title, podcast_title,
                                  audio_url, image_url, chunk_count)

        # Update in-memory cache — under lock to prevent "dict changed during iteration"
        # races with get_indexed_episodes() callers (get_library_data, /api/episodes).
        with self._episodes_lock:
            if self._episodes_cache is not None:
                self._episodes_cache[episode_id] = {
                    "title": episode_title,
                    "podcast_title": podcast_title,
                    "image_url": image_url,
                }
        self._library_cache = None
        self._library_cache_time = 0

    # ── Library Data ────────────────────────────────────────────────────

    def get_library_data(self) -> Dict:
        now = _time.time()
        if self._library_cache is not None and now - self._library_cache_time < 300:
            return self._library_cache

        episodes = self.get_indexed_episodes()
        podcasts: Dict[str, list] = {}
        for ep_id, ep in episodes.items():
            podcast = ep["podcast_title"]
            if podcast not in podcasts:
                podcasts[podcast] = []
            podcasts[podcast].append({
                "episode_id": ep_id,
                "title": ep["title"],
                "image_url": ep.get("image_url", ""),
            })

        for podcast in podcasts:
            podcasts[podcast].sort(key=lambda e: e["title"])

        result = {
            "podcasts": [
                {
                    "title": name,
                    "image_url": eps[0].get("image_url", "") if eps else "",
                    "episodes": eps,
                    "episode_count": len(eps),
                }
                for name, eps in sorted(podcasts.items())
            ]
        }
        self._library_cache = result
        self._library_cache_time = now
        return result

    # ── Search ──────────────────────────────────────────────────────────

    def _get_episode_context(self, episode_id: str, question: str, max_chunks: int = 20) -> list:
        """Retrieve the most relevant chunks within a single episode (Pinecone search
        filtered to that episode). Returns normalized match dicts."""
        return self._pc_search(question, top_k=max_chunks, episode_id=episode_id)

    def _build_sources(self, matches: list) -> List[Dict]:
        """Keep the highest-scoring chunk per episode for the source ('receipts') list.
        Operates on normalized match dicts from _pc_search."""
        best_per_episode: Dict[str, Dict] = {}
        for m in matches:
            episode_id = m.get("episode_id", "")
            score = round(m.get("score", 0) or 0, 3)
            if episode_id not in best_per_episode or score > best_per_episode[episode_id]["score"]:
                best_per_episode[episode_id] = {
                    "episode_id": episode_id,
                    "episode_title": m.get("episode_title", "Unknown"),
                    "podcast_title": m.get("podcast_title", "Unknown"),
                    "text": (m.get("text", "") or "")[:300],
                    "start_time": m.get("start_time"),
                    "end_time": m.get("end_time"),
                    "score": score,
                }
        return sorted(best_per_episode.values(), key=lambda s: s["score"], reverse=True)

    def get_transcript_segments(self, episode_id: str) -> Optional[List[Dict]]:
        """Retrieve full raw transcript segments from PostgreSQL podcast_transcripts."""
        try:
            with self._pg_cur() as cur:
                cur.execute(f"SELECT segments FROM {TRANSCRIPTS_TABLE} WHERE episode_id = %s", (episode_id,))
                row = cur.fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], list) else json.loads(row[0])
        except Exception as e:
            logger.warning("Could not load transcript segments: %s", e)
        return None

    def summarize_episode(self, episode_id: str) -> str:
        """A short, grounded summary of an episode for its detail page — built from a
        sample of the episode's own chunks. Raises on LLM failure; caller degrades."""
        # Use the stored transcript segments (Postgres) for an even spread across the
        # whole episode, rather than only the chunks a query surfaces.
        segs = self.get_transcript_segments(episode_id) or []
        title = "this episode"
        if not segs:
            hits = self._pc_search("overview summary", top_k=8, episode_id=episode_id)
            if not hits:
                return ""
            sample = [h.get("text", "") for h in hits]
            title = hits[0].get("episode_title", title)
        else:
            step = max(1, len(segs) // 8)
            sample = [s.get("text", "") for s in segs[::step]][:8]
        context = "\n\n".join(t for t in sample if t)
        resp = self.answer_client.chat.completions.create(
            model=self.answer_model,
            messages=[
                {"role": "system", "content":
                 "You write a concise, faithful overview of a podcast episode from excerpts of its own "
                 "transcript. 2-3 sentences, no preamble, no invented facts. Describe what's discussed."},
                {"role": "user", "content": f'Episode: "{title}"\n\nTranscript excerpts:\n{context}\n\nWrite the overview:'},
            ],
            max_tokens=600, temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()

    def reindex_all_to_pinecone(self) -> int:
        """Rebuild the vector index from saved Postgres transcripts — NO re-transcription.
        Used when switching vector store/embedding model. For each indexed episode,
        loads its stored segments and upserts them as text records to Pinecone."""
        eps = self.get_indexed_episodes()
        done = 0
        for ep_id, ep in list(eps.items()):
            segs = self.get_transcript_segments(ep_id)
            if not segs:
                logger.warning("No stored transcript for '%s' — skipping", ep.get("title", "")[:40])
                continue
            episode = {"title": ep["title"], "podcast_title": ep["podcast_title"]}
            try:
                self._upsert_chunks(episode, ep_id, segs)
                done += 1
                logger.info("Re-indexed '%s' (%d chunks)", ep["title"][:50], len(segs))
            except Exception as e:
                logger.error("Re-index failed for '%s': %s", ep["title"][:40], e)
        return done

    # ── Hybrid Search ───────────────────────────────────────────────────
    #
    # Why hybrid? Pure vector (semantic) search understands *meaning* but
    # underweights *exact tokens*. A passage literally naming "RevenueCat" can
    # rank below a passage that's merely about pricing, because the embedding
    # captures the topic, not the proper noun. So after the vector pull we add a
    # small keyword-overlap bonus to rescue exact names, numbers, and phrases —
    # most of the benefit of a heavyweight cross-encoder re-ranker, at almost no
    # cost. The boost is deliberately capped so it nudges ranking without
    # overriding genuine semantic relevance.

    def _keyword_boost(self, m: Dict, question_words: set) -> float:
        """Keyword-overlap bonus (0.0-0.15): +0.03 per query word found in the chunk's
        text or episode title, capped at 0.15. Added on top of the semantic score."""
        combined = (m.get("text", "") + " " + m.get("episode_title", "")).lower()
        hits = sum(1 for w in question_words if w in combined)
        return min(0.15, hits * 0.03)

    def _rerank_matches(self, matches: list, question: str) -> list:
        """Re-rank matches by semantic score + keyword overlap. Returns
        (combined_score, match) tuples sorted descending."""
        # Tokenize the question, drop stop-words so the boost keys on content words.
        question_words = set(re.sub(r'[^\w\s]', '', question.lower()).split())
        stop_words = {'what', 'how', 'the', 'is', 'are', 'was', 'were', 'do', 'does',
                      'did', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of', 'and',
                      'or', 'but', 'not', 'with', 'from', 'by', 'about', 'as', 'it',
                      'this', 'that', 'they', 'them', 'their', 'i', 'you', 'we', 'me'}
        question_words -= stop_words
        scored = [(m.get("score", 0) + self._keyword_boost(m, question_words), m) for m in matches]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    def search_and_answer(self, question: str, episode_id: str = None) -> Dict:
        """Hybrid search + grounded synthesis.

        1. Pinecone search (it embeds the query) → top 25 chunks
        2. Re-rank with a keyword boost (proper nouns, exact phrases)
        3. Dynamic threshold trims to the keepers
        4. Build context with timestamps (the citation metadata)
        5. The answer model writes a grounded, cited answer

        Returns: {"answer": str, "sources": [{"episode_id", "episode_title", ...}]}
        """
        if episode_id:
            matches = self._get_episode_context(episode_id, question)
        else:
            # Step 1: broad semantic retrieval (Pinecone embeds the query)
            results = self._pc_search(question, top_k=25)
            # Absolute floor: if even the best match is weak, the corpus doesn't
            # cover this — bail rather than feed the model noise. (Scores from the
            # integrated model run lower than raw cosine, so the floor is modest.)
            if not results or results[0]["score"] < 0.08:
                return {"answer": "No relevant content found for this query.", "sources": []}
            # Step 2: keyword re-rank → (combined_score, match) tuples
            reranked = self._rerank_matches(results, question)
            # Step 3: dynamic relative threshold — keep within 55% of the best
            # combined score, capped at 15 excerpts.
            best_score = reranked[0][0] if reranked else 0
            threshold = max(0.05, best_score * 0.55)
            matches = [m for combined, m in reranked if combined >= threshold][:15]

        if not matches:
            return {"answer": "No relevant content found for this query.", "sources": []}

        # Step 4: build the context block — each excerpt labelled with its episode +
        # timestamp so the model can cite it and the claim stays checkable.
        context_parts = []
        for i, m in enumerate(matches, 1):
            text = m.get("text", "")
            episode = m.get("episode_title", "Unknown Episode")
            start = m.get("start_time")
            header = f'[EXCERPT {i}]\nEpisode: "{episode}"'
            if start is not None:
                mins, secs = int(float(start) // 60), int(float(start) % 60)
                header += f"\nTimestamp: {mins}:{secs:02d}"
            context_parts.append(f"{header}\nContent: {text}")

        # Step 5: LLM generates grounded answer
        context = "\n\n---\n\n".join(context_parts)
        response = self.answer_client.chat.completions.create(
            model=self.answer_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(context, question)},
            ],
            max_tokens=1500,   # headroom for "thinking" models that reason before answering
            temperature=0.3,
        )

        return {
            "answer": response.choices[0].message.content,
            "sources": self._build_sources(matches),
        }

    # ── RSS & Transcription ─────────────────────────────────────────────

    def _get_rss_url(self, podcast_url: str) -> str:
        if "podcasts.apple.com" in podcast_url:
            podcast_id = podcast_url.split("/id")[-1].split("?")[0]
            url = f"https://itunes.apple.com/lookup?id={podcast_id}"
            # iTunes API can flake with rate limits, transient 5xx, or timeouts.
            # Retry 3x with exponential backoff (1s, 2s, 4s) so the crawler
            # doesn't permanently lose this feed on a single network blip.
            for attempt in range(3):
                try:
                    r = self._http.get(url, timeout=10)
                    r.raise_for_status()
                    response = r.json()
                    if response.get("results"):
                        return response["results"][0].get("feedUrl")
                    return podcast_url
                except Exception as e:
                    if attempt == 2:
                        logger.warning("iTunes lookup failed for %s after 3 attempts: %s",
                                       podcast_id, e)
                        return podcast_url
                    _time.sleep(2 ** attempt)
        return podcast_url

    def _fetch_episodes(self, rss_url: str) -> List[Dict]:
        feed = feedparser.parse(self._http.get(rss_url, timeout=30).text)
        if not feed.entries:
            return []

        podcast_title = getattr(feed.feed, "title", "Unknown Podcast")
        podcast_image = ""
        if hasattr(feed.feed, "image") and hasattr(feed.feed.image, "href"):
            podcast_image = feed.feed.image.href

        episodes = []
        for entry in feed.entries:
            audio_url = self._extract_audio_url(entry)
            if not audio_url:
                continue
            image_url = (
                entry.image["href"]
                if hasattr(entry, "image") and "href" in entry.image
                else podcast_image
            )
            episodes.append({
                "title": entry.title,
                "audio_url": audio_url,
                "podcast_title": podcast_title,
                "image_url": image_url,
            })
        return episodes

    @staticmethod
    def _extract_audio_url(entry) -> Optional[str]:
        """Extract audio URL from an RSS feed entry."""
        for link in getattr(entry, "links", []):
            if "audio" in getattr(link, "type", "") or any(
                ext in getattr(link, "href", "").lower() for ext in [".mp3", ".m4a"]
            ):
                return link.href
        for enc in getattr(entry, "enclosures", []):
            if "audio" in getattr(enc, "type", ""):
                return enc.href
        return None

    # ── Local Pipeline ──────────────────────────────────────────────────

    # MIME type → file extension, for audio formats podcasts actually use.
    _AUDIO_MIME_EXT = {
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a",
        "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/flac": ".flac",
    }

    def _download_audio(self, audio_url: str) -> Optional[str]:
        """Download audio to local cache. Returns filepath or None on failure.

        Filename is keyed by URL hash, so we need a stable filename BEFORE we
        download (to check the cache). We pick a tentative extension from the
        URL, then rename the file after the response arrives if the actual
        Content-Type indicates a different format (e.g., feed says .mp3 but
        server returns audio/mp4 with a redirect)."""
        url_lower = audio_url.lower().split("?")[0]  # strip query strings
        if ".m4a" in url_lower or ".aac" in url_lower:
            tentative_ext = ".m4a"
        elif ".wav" in url_lower:
            tentative_ext = ".wav"
        else:
            tentative_ext = ".mp3"

        url_hash = hashlib.md5(audio_url.encode()).hexdigest()
        filepath = os.path.join(AUDIO_CACHE_DIR, url_hash + tentative_ext)

        # Already-cached check considers all known audio extensions
        for ext in (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".webm"):
            cached = os.path.join(AUDIO_CACHE_DIR, url_hash + ext)
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                logger.info("Audio cached: %s", os.path.basename(cached))
                return cached

        logger.info("Downloading audio: %s", audio_url[:80])
        try:
            r = self._http.get(audio_url, stream=True, timeout=(30, 600))
            r.raise_for_status()
            # Use the server's Content-Type to pick the real extension
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            real_ext = self._AUDIO_MIME_EXT.get(mime, tentative_ext)
            filepath = os.path.join(AUDIO_CACHE_DIR, url_hash + real_ext)
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            if os.path.getsize(filepath) == 0:
                os.remove(filepath)
                return None
            logger.info("Downloaded: %s (%dMB)", os.path.basename(filepath), os.path.getsize(filepath) >> 20)
            return filepath
        except Exception as e:
            logger.error("Download failed: %s", e)
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

    # ── Transcription via the gateway (/v1/transcribe-url) ────────────────────
    # the gateway serves Parakeet remotely. Submit a public audio URL → poll for job
    # completion → get a single text blob + duration_sec. For audio >45 MB
    # (the gateway's hard cap is 50 MB), pre-compress with ffmpeg to 16 kHz mono Opus
    # ~32 kbps and host the result behind Caddy at LIVE_AUDIO_BASE_URL so
    # the gateway can fetch it over public HTTPS.

    def _compress_audio_for_gateway(self, local_path: str) -> Optional[List[Tuple[str, str]]]:
        """ffmpeg → 16 kHz mono Opus 32 kbps in .webm, SPLIT into ≤25-min segments.

        Parakeet's GPU memory scales with audio DURATION (a 2-hour episode tries
        to allocate ~24 GB of Metal buffer; Mac mini's cap is 8 GB). Splitting
        client-side keeps every submitted chunk well under that ceiling.

        the gateway /v1/transcribe-url accepts webm container (NOT raw .opus extension).
        Returns list of (compressed_local_path, public_url) in order, or None.
        """
        token = _uuid.uuid4().hex
        pattern = os.path.join(TEMP_AUDIO_DIR, f"{token}_%03d.webm")
        cmd = ["ffmpeg", "-y", "-i", local_path, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "libopus", "-b:a", "32k",
               # Parakeet on the Mac mini's 8GB Metal GPU OOMs above ~4 min audio
               # (tested empirically: 4-min works in ~80s, 5-min crashes the
               # Parakeet process). 240s gives us safety margin.
               "-f", "segment", "-segment_time", "240", "-reset_timestamps", "1",
               pattern]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=600)
            if r.returncode != 0:
                logger.error("ffmpeg failed (rc=%d): %s", r.returncode,
                             r.stderr[-400:].decode("utf-8", "ignore"))
                return None
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("ffmpeg invocation failed: %s", e)
            return None
        import glob
        paths = sorted(glob.glob(os.path.join(TEMP_AUDIO_DIR, f"{token}_*.webm")))
        if not paths:
            logger.error("ffmpeg produced no output chunks")
            return None
        total_mb = sum(os.path.getsize(p) for p in paths) / (1024 * 1024)
        logger.info("ffmpeg compressed+split: %d chunks, %.1f MB total", len(paths), total_mb)
        return [(p, f"{LIVE_AUDIO_BASE_URL}/{os.path.basename(p)}") for p in paths]

    def _gateway_submit_and_poll(self, submit_url: str) -> Tuple[Optional[str], float, str]:
        """Submit one audio URL to the gateway, poll until done.
        Returns (text, duration_sec, error_reason). Empty error on success.

        Submit is retried up to 3 times with exponential backoff on transient
        errors (timeout / 502 / 503 / 429) — important because we send ~30
        chunks per long episode, and ONE chunk's submit failure aborts the
        whole episode."""
        headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
        job_id = None
        last_err = ""
        for attempt in range(3):
            try:
                r = self._http.post(f"{LLM_BASE_URL}/v1/transcribe-url",
                                    headers=headers, json={"url": submit_url}, timeout=SUBMIT_TIMEOUT)
                if r.status_code == 202:
                    job_id = r.json()["job_id"]
                    logger.info("Transcription job: %s (audio=%s)", job_id, submit_url[-60:])
                    break
                # Transient: retry. Permanent: bail immediately.
                if r.status_code in (429, 502, 503):
                    last_err = f"submit {r.status_code}: {r.text[:120]}"
                    logger.warning("Submit transient (%s), retry %d/3 in %ds", r.status_code, attempt + 1, 2 ** attempt)
                    _time.sleep(2 ** attempt)
                    continue
                return None, 0.0, f"submit {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last_err = f"submit error: {e}"
                if attempt < 2:
                    logger.warning("Submit exception (%s), retry %d/3 in %ds", e, attempt + 1, 2 ** attempt)
                    _time.sleep(2 ** attempt)
                    continue
        if not job_id:
            return None, 0.0, f"submit failed after 3 retries: {last_err}"

        deadline = _time.time() + JOB_TIMEOUT
        last_status = ""
        while _time.time() < deadline:
            _time.sleep(POLL_INTERVAL)
            try:
                s = self._http.get(f"{LLM_BASE_URL}/v1/transcribe-url/{job_id}",
                                   headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=30)
                s.raise_for_status()
                data = s.json()
            except Exception as e:
                logger.warning("poll error (continuing): %s", e); continue
            status = data.get("status", "")
            if status != last_status:
                logger.info("Transcription %s: %s", job_id, status); last_status = status
            if status == "done":
                return data.get("text", ""), float(data.get("duration_sec", 0)), ""
            if status == "error":
                return None, 0.0, f"transcription error: {data.get('error','unknown')}"
        return None, 0.0, f"transcription job {job_id} did not complete within {JOB_TIMEOUT}s"

    def _transcribe(self, audio_url: str, podcast_title: str, episode_title: str
                             ) -> Tuple[Optional[List[Dict]], str]:
        """Download → compress + split into ≤25-min chunks → submit each to the gateway
        → concatenate transcripts → chunk text into search segments.
        Returns (segments, error_reason). speakers is always [] (the gateway doesn't diarize).
        """
        ep_id = self._episode_id(podcast_title, episode_title)

        # Step 0: crash-recovery — reuse stored transcript if we have one
        try:
            with self._pg_cur() as cur:
                cur.execute(f"SELECT segments FROM {TRANSCRIPTS_TABLE} WHERE episode_id = %s", (ep_id,))
                row = cur.fetchone()
            if row and row[0]:
                logger.info("Using stored transcript: %s", episode_title[:50])
                return row[0], ""
        except Exception as e:
            logger.warning("Could not load stored transcript, re-transcribing: %s", e)

        # Step 1: download (we ALWAYS download and chunk — keeps the pipeline
        # deterministic regardless of audio length / HEAD reliability / vendor
        # rate limits. Small extra bandwidth cost; large reliability win.)
        downloaded = self._download_audio(audio_url)
        if not downloaded:
            return None, f"download failed for {audio_url[:80]}"

        # Step 2: ffmpeg compress + split into ≤25-min chunks
        chunks = self._compress_audio_for_gateway(downloaded)
        try: os.remove(downloaded)
        except OSError: pass
        if not chunks:
            return None, "ffmpeg compression/split failed"

        # Step 3: submit each chunk sequentially, collect text + duration
        full_text_parts: List[str] = []
        full_duration = 0.0
        try:
            for i, (local_path, submit_url) in enumerate(chunks, 1):
                sz_mb = os.path.getsize(local_path) / (1024 * 1024)
                if sz_mb > 50:
                    return None, f"chunk {i}/{len(chunks)} is {sz_mb:.1f} MB (over 50 MB cap)"
                logger.info("Transcribing chunk %d/%d (%.1f MB)", i, len(chunks), sz_mb)
                text, dur, err = self._gateway_submit_and_poll(submit_url)
                if err:
                    return None, f"chunk {i}/{len(chunks)}: {err}"
                if text:
                    full_text_parts.append(text.strip())
                full_duration += dur
        finally:
            for path, _ in chunks:
                self._cleanup_temp(path)

        # Step 4: join transcripts + chunk into search segments with proportional times
        full_text = " ".join(p for p in full_text_parts if p)
        if not full_text:
            return None, "all chunks transcribed but joined text is empty"
        segments = self._chunk_text(full_text, full_duration)
        if not segments:
            return None, "text chunking produced no segments"
        logger.info("Done: %d segments from %d audio chunks (%.1f s total)",
                    len(segments), len(chunks), full_duration)
        return segments, ""

    @staticmethod
    def _cleanup_temp(path: Optional[str]) -> None:
        """Best-effort delete of a temp audio file. Logs on failure but never raises."""
        if not path: return
        try: os.remove(path)
        except OSError as e: logger.warning("temp cleanup failed for %s: %s", path, e)

    # ── Text chunking ───────────────────────────────────────────────────
    #
    # Chunk size is a classic retrieval trade-off: too large and a chunk mixes
    # several topics (noisy embedding, imprecise citation); too small and a single
    # thought gets split across chunks (the match loses its context). ~1,000 chars
    # — a few sentences — is a good default: big enough to hold one idea, small
    # enough to point at. We split on sentence boundaries so chunks never cut a
    # sentence in half.

    @staticmethod
    def _chunk_text(text: str, duration_sec: float, target_size: int = 1000) -> List[Dict]:
        """Split a transcript into ~target_size-char chunks at sentence boundaries,
        assigning each chunk a proportional start/end time from its character
        position. `speakers` is always [] — the transcription model doesn't diarize."""
        if not text.strip(): return []
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        groups, cur, cur_len = [], [], 0
        for s in sentences:
            # Close the current chunk once adding this sentence would overflow the
            # target, then start a fresh one. (Greedy packing, sentence-aligned.)
            if cur_len + len(s) > target_size and cur:
                groups.append(" ".join(cur)); cur, cur_len = [], 0
            cur.append(s); cur_len += len(s) + 1
        if cur: groups.append(" ".join(cur))
        # We don't have per-word timestamps after joining, so approximate each
        # chunk's start/end by its share of the total character count. Good enough
        # to jump the listener to roughly the right moment in a long episode.
        total_chars = sum(len(g) for g in groups) or 1
        result, cursor = [], 0
        for g in groups:
            start = (cursor / total_chars) * duration_sec
            cursor += len(g)
            end = (cursor / total_chars) * duration_sec
            result.append({"text": g, "start_time": start, "end_time": end, "speakers": []})
        return result

    def _save_transcript(self, segments: List[Dict], episode_id: str, episode_title: str):
        """Save full raw transcript to PostgreSQL podcast_transcripts table.
        Raises on failure — caller must handle (transcript is the safety net)."""
        with self._pg_cur() as cur:
            cur.execute(f"""
                INSERT INTO {TRANSCRIPTS_TABLE} (episode_id, segments)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (episode_id) DO UPDATE SET segments = EXCLUDED.segments, created_at = NOW()
            """, (episode_id, json.dumps(segments)))
        logger.info("Saved transcript to PostgreSQL: %s (%d segments)", episode_title[:50], len(segments))

    def _upsert_chunks(self, episode: Dict, episode_id: str, chunk_data: List[Dict]):
        """Upsert an episode's chunks to Pinecone as TEXT records. Pinecone's
        integrated model embeds the `text` field — we never compute vectors. IDs are
        deterministic (`<episode_id>_c<NNNN>`) so re-indexing overwrites cleanly."""
        records = []
        for i, c in enumerate(chunk_data):
            records.append({
                "_id":            self._chunk_id(episode_id, i),
                PINECONE_TEXT_FIELD: c["text"],          # the field Pinecone embeds
                "episode_id":     episode_id,
                "episode_title":  episode["title"],
                "podcast_title":  episode["podcast_title"],
                "chunk_index":    i,
                "start_time":     float(c.get("start_time") or 0),
                "end_time":       float(c.get("end_time") or 0),
            })
        self._pc_upsert(records)

    # ── Processing Pipeline ─────────────────────────────────────────────

    def _process_single_episode(self, episode: Dict, episode_id: str,
                                status_callback=None) -> Tuple[bool, str]:
        """Process a single episode. Returns (success, error_reason)."""
        title = episode["title"]

        if status_callback:
            status_callback("transcribing", title, "Transcribing audio...")
        segments, error_reason = self._transcribe(
            episode["audio_url"], episode["podcast_title"], title
        )
        if not segments:
            if status_callback:
                status_callback("error", title, error_reason or "Could not process audio")
            return False, error_reason

        # _transcribe already returns chunked segments — no extra step.
        chunk_data = segments

        if status_callback:
            status_callback("saving", title, f"Indexing {len(chunk_data)} chunks...")
        try:
            # Order matters: episode row must exist before transcript (FK constraint)
            self._upsert_episode_catalog(
                episode_id, title, episode["podcast_title"],
                episode.get("audio_url", ""),
                episode.get("image_url", ""),
                len(chunk_data),
            )
            self._save_transcript(segments, episode_id, title)
            # Pinecone embeds the chunk text on upsert — no local embedding step.
            self._upsert_chunks(episode, episode_id, chunk_data)
        except Exception as e:
            logger.error("DB upsert failed for '%s': %s", title[:50], e)
            if status_callback:
                status_callback("error", title, f"DB upload failed: {str(e)[:100]}")
            return False, f"DB upload failed: {str(e)[:150]}"

        logger.info("Indexed episode: %s (%d chunks)", title, len(chunk_data))
        return True, ""

    def auto_process_episodes(self, status_callback=None, max_episodes=5) -> int:
        unlimited = (max_episodes is None or max_episodes == 0)

        indexed_ids = set(self.get_indexed_episodes().keys())

        # Gather episodes from all podcasts in parallel (RSS feeds are independent)
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        def _fetch_podcast(args):
            idx, url = args
            for attempt in range(3):
                try:
                    rss_url = self._get_rss_url(url)
                    eps = self._fetch_episodes(rss_url)
                    return idx, eps, None
                except Exception as e:
                    if attempt == 2:
                        return idx, [], str(e)
                    _time.sleep(2 ** attempt)

        per_podcast = [None] * len(DEFAULT_PODCAST_URLS)
        with ThreadPoolExecutor(max_workers=min(4, len(DEFAULT_PODCAST_URLS))) as ex:
            futures = [ex.submit(_fetch_podcast, (i, url)) for i, url in enumerate(DEFAULT_PODCAST_URLS)]
            for fut in _as_completed(futures):
                idx, eps, err = fut.result()
                per_podcast[idx] = eps
                if err:
                    logger.error("RSS fetch failed for podcast %d: %s", idx + 1, err)
                    if status_callback:
                        status_callback("error", "", f"Podcast {idx + 1} failed: {err[:80]}")
                elif status_callback:
                    name = eps[0]["podcast_title"] if eps else f"Podcast {idx + 1}"
                    status_callback("discovery", "", f"{name}: {len(eps)} episodes")

        # Round-robin interleave across shows: take episode 1 from every show, then
        # episode 2 from every show, and so on (zip_longest pads uneven feeds with
        # None, which the `if ep` filters out). This way a 900-episode feed doesn't
        # monopolize the queue — breadth across shows beats depth into any one.
        all_episodes = [ep for batch in zip_longest(*per_podcast) for ep in batch if ep]

        if status_callback:
            status_callback(
                "discovery", "",
                f"{len(all_episodes)} episodes across {len(per_podcast)} podcasts, "
                f"{len(indexed_ids)} indexed",
            )

        # Audio URL dedup: track seen URLs to catch title-change phantom duplicates
        seen_audio_urls: set = set()

        # Per-podcast consecutive failure tracking
        podcast_consecutive_fails: Dict[str, int] = {}
        FAIL_THRESHOLD = 3

        processed = 0
        for ep in all_episodes:
            podcast_name = ep["podcast_title"]
            episode_id = self._episode_id(podcast_name, ep["title"])
            audio_url = ep.get("audio_url", "")

            # Already indexed — record audio URL for dedup and skip
            if episode_id in indexed_ids:
                if audio_url:
                    seen_audio_urls.add(audio_url)
                continue

            if self.episode_exists(episode_id):
                indexed_ids.add(episode_id)
                if audio_url:
                    seen_audio_urls.add(audio_url)
                continue

            # Audio URL dedup: same audio already indexed under different title hash
            if audio_url and audio_url in seen_audio_urls:
                logger.info("Skipping title-change duplicate: %s", ep["title"][:50])
                if status_callback:
                    status_callback("skipped", ep["title"],
                                    "Duplicate audio URL — already indexed under different title")
                continue
            if audio_url:
                seen_audio_urls.add(audio_url)

            # Per-podcast failure threshold: skip after 3 consecutive failures
            if podcast_consecutive_fails.get(podcast_name, 0) >= FAIL_THRESHOLD:
                if status_callback:
                    status_callback("skipped", ep["title"],
                                    f"Skipping — {podcast_name[:30]} hit {FAIL_THRESHOLD} consecutive failures")
                continue

            if status_callback:
                status_callback("fetching", ep["title"], f"Starting episode ({podcast_name[:30]})")

            try:
                success, error_reason = self._process_single_episode(ep, episode_id, status_callback)
                if success:
                    processed += 1
                    indexed_ids.add(episode_id)
                    podcast_consecutive_fails[podcast_name] = 0
                    if status_callback:
                        status_callback("done", ep["title"], "Episode indexed successfully")
                    # Cooldown only after successful heavy compute — lets ANE/GPU thermals settle
                    _time.sleep(30)
                else:
                    logger.warning("Skipping '%s': %s", ep["title"][:50], error_reason)
                    podcast_consecutive_fails[podcast_name] = \
                        podcast_consecutive_fails.get(podcast_name, 0) + 1
            except Exception as e:
                logger.error("Failed to process '%s': %s", ep["title"][:50], e)
                if status_callback:
                    status_callback("error", ep["title"], str(e)[:120])
                podcast_consecutive_fails[podcast_name] = \
                    podcast_consecutive_fails.get(podcast_name, 0) + 1

            if not unlimited and processed >= max_episodes:
                break

        if status_callback:
            status_callback("complete", "", f"Finished. {processed} new episodes processed.")

        return processed


# ── Singleton ───────────────────────────────────────────────────────

_engine_lock = threading.Lock()
_engine_instance = None


def get_engine() -> PodcastEngine:
    """Thread-safe singleton. Shared by app.py and MCP server."""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = PodcastEngine()
    return _engine_instance


if __name__ == "__main__":
    import argparse
    import time
    import urllib.request, urllib.error
    import json as _json
    import socket

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="python transcribe.py --episodes 5")
    parser.add_argument("--episodes", type=int, default=5)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--reindex", action="store_true",
                        help="Rebuild the vector index from saved transcripts (no re-transcription)")
    args = parser.parse_args()

    engine = get_engine()

    if args.reindex:
        n = engine.reindex_all_to_pinecone()
        logger.info("Re-indexed %d episodes into Pinecone.", n)
        raise SystemExit(0)

    max_episodes = None if args.all else args.episodes
    logger.info("Episodes to process: %s", "all" if args.all else args.episodes)
    logger.info("%d already indexed", len(engine.get_indexed_episodes()))

    # ── Live relay setup (so standalone `transcribe.py --all` also pushes
    # events to the production live site). Mirrors the relay in app.py for
    # the standalone path. Fire-and-forget, batched 1-second.
    from config import LIVE_RELAY_URL, MCP_API_KEYS

    _RELAY_PHASES = frozenset({"discovery", "fetching", "transcribing",
                               "embedding", "saving", "done", "complete"})
    _relay_url = (LIVE_RELAY_URL or "").rstrip("/")
    _relay_auth = (MCP_API_KEYS.split(",")[0] if MCP_API_KEYS else "").strip()
    _relay_enabled = bool(_relay_url and _relay_auth)
    _relay_buffer: list = []
    _relay_lock = threading.Lock()
    _relay_source = re.sub(r"[^a-zA-Z0-9-]", "", socket.gethostname().split(".")[0])[:32] or "local"

    def _relay_push(phase, episode, message):
        if not _relay_enabled or phase not in _RELAY_PHASES:
            return
        with _relay_lock:
            if len(_relay_buffer) >= 200:
                _relay_buffer.pop(0)
            _relay_buffer.append({"phase": phase,
                                  "episode": (episode or "")[:200],
                                  "message": (message or "")[:500]})

    def _relay_flush_loop():
        url = f"{_relay_url}/api/crawler/ingest"
        headers = {"Content-Type": "application/json", "X-API-Key": _relay_auth}
        while True:
            time.sleep(1.0)
            with _relay_lock:
                if not _relay_buffer:
                    continue
                batch = _relay_buffer[:]
                _relay_buffer.clear()
            try:
                body = _json.dumps({"source": _relay_source, "events": batch}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()
            except Exception as e:
                logger.debug("Relay POST failed (%d events dropped): %s", len(batch), e)

    if _relay_enabled:
        threading.Thread(target=_relay_flush_loop, daemon=True, name="LiveRelay").start()
        logger.info("Live relay: %s → %s", _relay_source, _relay_url)

    def _status_cb(phase, ep, msg):
        logger.info("[%s] %s — %s", phase, ep[:50] if ep else "", msg)
        _relay_push(phase, ep, msg)

    start = time.time()
    processed = engine.auto_process_episodes(
        status_callback=_status_cb,
        max_episodes=max_episodes,
    )
    logger.info("Done. %d episodes in %.1fs", processed, time.time() - start)
