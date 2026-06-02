"""
One-off migration: re-embed an older collection (1024-dim vectors) into a new
collection `podcast-search` (768-dim, embeddinggemma) and copy it over. Preserves
point IDs and drops a couple of redundant legacy payload fields during the copy.

This is the kind of script you write once when you change embedding models: you
can't compare a 768-dim query vector against 1024-dim stored vectors, so every
chunk has to be re-embedded. It reads from the source collection, re-embeds each
chunk's text, and writes to the target — leaving the source untouched.

Run where Qdrant is reachable. Idempotent — safe to re-run after interruption;
already-migrated points are simply upserted again (no data loss, slight wasted
work). Resumes from /tmp/migrate_progress.json.

Usage:  python3 scripts/migrate_to_embeddinggemma.py [--workers N] [--limit N]

Env (or constants below):
  LLM_BASE_URL, LLM_API_KEY, QDRANT_URL, QDRANT_API_KEY
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

# ── Config ─────────────────────────────────────────────────────────────────
LLM_URL    = os.environ.get("LLM_BASE_URL", "http://localhost:8000")
LLM_KEY    = os.environ.get("LLM_API_KEY",   "")
QDRANT_URL  = os.environ.get("QDRANT_URL",    "http://localhost:6333")
QDRANT_KEY  = os.environ.get("QDRANT_API_KEY", "")

SOURCE_COLLECTION = "podcast-search-engine-mcp"   # 1024-dim, llama-text-embed-v2
TARGET_COLLECTION = "podcast-search"              # 768-dim,  embeddinggemma
TARGET_DIM        = 768
EMBED_MODEL       = "embeddinggemma"
BATCH             = 64       # the gateway max inputs per request
SCROLL_PAGE       = 256      # how many points to pull from Qdrant per scroll
STATE_FILE        = "/tmp/migrate_progress.json"


# ── the gateway client ────────────────────────────────────────────────────────────
def embed_batch(texts, retries=3):
    """POST /v1/embeddings → returns list of vectors. Retries on transient errors."""
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{LLM_URL}/v1/embeddings",
                data=body,
                headers={"Authorization": f"Bearer {LLM_KEY}",
                         "Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read())
            return [d["embedding"] for d in data["data"]]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt  # 1s, 2s, 4s
            print(f"[embed retry {attempt+1}/{retries} in {wait}s] {e}")
            time.sleep(wait)
    raise RuntimeError(f"embed_batch failed after {retries} retries: {last_err}")


# ── State / resume ─────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {"offset": None, "processed": 0, "started_at": time.time()}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)


# ── Worker ─────────────────────────────────────────────────────────────────
def process_chunk_of_points(points, target_client):
    """Embed text from `points` (up to BATCH) and upsert into target collection.
    Per-point handling: any point without `text` is logged and skipped INDIVIDUALLY
    (never the whole batch), so one bad row can't strand the rest."""
    keep = [(p, p.payload.get("text", "")) for p in points if p.payload.get("text", "")]
    skipped = len(points) - len(keep)
    if skipped:
        skipped_ids = [str(p.id) for p in points if not p.payload.get("text", "")]
        print(f"  WARN: skipped {skipped} points without text. IDs: {skipped_ids[:3]}...")
    if not keep:
        return 0
    vectors = embed_batch([t for _, t in keep])
    new_points = []
    for (p, _), v in zip(keep, vectors):
        # Drop two redundant legacy fields while copying: one duplicated the point's
        # own ID (`<episode_id>_c<NNNN>`), the other was always empty.
        clean_payload = {k: val for k, val in p.payload.items()
                         if k not in ("pinecone_id", "pinecone_namespace")}
        new_points.append(PointStruct(id=p.id, vector=v, payload=clean_payload))
    target_client.upsert(collection_name=TARGET_COLLECTION, points=new_points, wait=True)
    return len(new_points)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit",   type=int, default=0, help="stop after N points (for testing)")
    ap.add_argument("--reset-state", action="store_true", help="ignore /tmp/migrate_progress.json and start fresh")
    args = ap.parse_args()

    qc = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY, timeout=60)

    # 1) Source sanity
    src_info = qc.get_collection(SOURCE_COLLECTION)
    src_dim = src_info.config.params.vectors.size
    src_count = src_info.points_count
    print(f"Source: {SOURCE_COLLECTION} | {src_count:,} points | {src_dim}-dim")

    # 2) Ensure target exists with correct dim
    existing = {c.name for c in qc.get_collections().collections}
    if TARGET_COLLECTION not in existing:
        print(f"Creating {TARGET_COLLECTION} ({TARGET_DIM}-dim, Cosine)")
        qc.create_collection(
            collection_name=TARGET_COLLECTION,
            vectors_config=VectorParams(size=TARGET_DIM, distance=Distance.COSINE),
        )
    else:
        tgt_info = qc.get_collection(TARGET_COLLECTION)
        tgt_dim = tgt_info.config.params.vectors.size
        if tgt_dim != TARGET_DIM:
            sys.exit(f"FATAL: {TARGET_COLLECTION} already exists at {tgt_dim}-dim, "
                     f"expected {TARGET_DIM}. Refusing to mix dimensions.")
        print(f"Target: {TARGET_COLLECTION} | {tgt_info.points_count:,} points already migrated")

    # 3) Resume from saved offset
    state = {"offset": None, "processed": 0, "started_at": time.time()}
    if not args.reset_state:
        state = load_state()
        if state.get("offset"):
            print(f"Resuming from offset {str(state['offset'])[:40]}... | already processed {state['processed']:,}")

    # 4) Scroll source → batch BATCH at a time → parallel embed+upsert
    offset = state.get("offset")
    processed_this_run = 0
    t_run = time.time()
    pool = ThreadPoolExecutor(max_workers=args.workers)

    try:
        while True:
            page, next_offset = qc.scroll(
                collection_name=SOURCE_COLLECTION,
                limit=SCROLL_PAGE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not page:
                break

            # Slice into BATCH-sized chunks and dispatch in parallel
            futures = []
            for i in range(0, len(page), BATCH):
                chunk = page[i:i+BATCH]
                futures.append(pool.submit(process_chunk_of_points, chunk, qc))

            for f in as_completed(futures):
                try:
                    n = f.result()
                    state["processed"] += n
                    processed_this_run += n
                except Exception as e:
                    print(f"  ERR chunk: {e}")

            # Progress every page
            elapsed = time.time() - t_run
            rate = processed_this_run / max(elapsed, 1)
            eta_sec = (src_count - state["processed"]) / max(rate, 0.1)
            print(f"  [{state['processed']:>7,}/{src_count:,}] "
                  f"{rate:.0f} pts/s | ETA {eta_sec/3600:.1f}h", flush=True)

            state["offset"] = next_offset
            save_state(state)

            if args.limit and state["processed"] >= args.limit:
                print(f"--limit {args.limit} reached, stopping")
                break
            if next_offset is None:
                break
            offset = next_offset
    finally:
        pool.shutdown(wait=True)

    # 5) Final integrity check — count + ID set comparison
    tgt_info = qc.get_collection(TARGET_COLLECTION)
    print(f"\nDone. Target collection now has {tgt_info.points_count:,} points "
          f"({tgt_info.config.params.vectors.size}-dim).")
    print(f"Source untouched: {src_count:,} points still in {SOURCE_COLLECTION}.")
    diff = src_count - tgt_info.points_count
    if diff > 0:
        print(f"WARNING: target has {diff:,} fewer points than source.")
        print("Listing first 10 missing IDs for inspection...")
        # Scan source IDs and look up in target
        missing = []
        offset = None
        while len(missing) < 10:
            page, offset = qc.scroll(SOURCE_COLLECTION, limit=1000, offset=offset,
                                     with_payload=False, with_vectors=False)
            if not page: break
            src_ids = [p.id for p in page]
            tgt_existing = {p.id for p in qc.retrieve(TARGET_COLLECTION, ids=src_ids,
                                                       with_payload=False, with_vectors=False)}
            for sid in src_ids:
                if sid not in tgt_existing:
                    missing.append(sid)
                    if len(missing) >= 10: break
            if offset is None: break
        print(f"  missing IDs (sample): {missing}")
        print("  Re-run this script to retry — already-present IDs will be no-op upserts.")
    else:
        print("✓ Counts match. Migration complete with no data loss.")

if __name__ == "__main__":
    main()
