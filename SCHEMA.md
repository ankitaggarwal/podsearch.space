# Data model

Podcast Search keeps facts in **PostgreSQL** and similarity in **Pinecone**. Postgres
is the source of truth for the catalog and the raw transcripts; Pinecone holds the
chunk vectors (which it embeds) used for retrieval. Every record carries enough metadata
to trace a match back to the exact episode and moment.

Create the Postgres tables once with [`schema.sql`](schema.sql). The table names
can be **prefixed** (set `DB_TABLE_PREFIX`) so several projects can share one
database — see the note in `schema.sql`.

---

## PostgreSQL

### `podcast_episodes` — the catalog

One row per indexed episode. Read constantly (served from an in-memory cache,
refreshed on a 2-minute TTL), written once per episode at index time.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `text` (PK) | `md5(podcast_title + "_" + episode_title)` — stable across re-indexes |
| `title` | `text` | episode title |
| `podcast_title` | `text` | the show the episode belongs to |
| `audio_url` | `text` | resolved audio URL (used for dedup) |
| `image_url` | `text` | episode/show artwork, if any |
| `chunk_count` | `int` | number of chunks indexed for this episode |
| `indexed_at` | `timestamptz` | set/updated when the episode is (re)indexed |

Upsert is `ON CONFLICT (id) DO UPDATE` so re-indexing an episode is idempotent;
`audio_url` is preserved if a later pass arrives without one.

### `podcast_transcripts` — the raw transcripts

One row per episode, holding the full transcript as JSON. This is the safety net:
chunks can always be rebuilt from here without re-transcribing.

| Column | Type | Notes |
|--------|------|-------|
| `episode_id` | `text` (PK → `podcast_episodes.id`) | |
| `segments` | `jsonb` | array of utterance segments (below) |
| `created_at` | `timestamp` | set/updated on write |

Each element of `segments`:

```json
{
  "text": "…the passage text…",
  "start_time": 2832.0,
  "end_time": 2910.5,
  "speakers": []
}
```

> `speakers` is always `[]` today — the transcription model (Parakeet) does not
> diarize. The field is kept in the shape so speaker labels can be added later
> without a migration.

---

## Pinecone (integrated embedding)

A single Pinecone index with an **integrated embedding model**, so the app upserts and
queries **text** — Pinecone computes the vectors. We never send embeddings over the wire.

| Property | Value |
|----------|-------|
| Index | `podsearch-space` (host via `PINECONE_HOST`) |
| Embedding model | `llama-text-embed-v2` (integrated, hosted by Pinecone) |
| Vector size | **768** |
| Distance | **Cosine** |
| Embedded field | `text` (set by `PINECONE_TEXT_FIELD`) — Pinecone embeds this on upsert |
| Record ID | deterministic string `<episode_id>_c<NNNN>` — re-indexing overwrites cleanly |

### Record fields (per chunk)

| Field | Type | Used for |
|-------|------|----------|
| `text` | `str` | **embedded by Pinecone**; also the excerpt shown / sent to the LLM |
| `episode_id` | `str` | metadata filter to a single episode; join back to Postgres |
| `episode_title` | `str` | display + keyword re-rank |
| `podcast_title` | `str` | display |
| `chunk_index` | `int` | order within the episode |
| `start_time` | `float` | seconds — the "jump to the moment" timestamp |
| `end_time` | `float` | seconds |

### How the two stores work together

- **Search** sends the question text to Pinecone (which embeds it), pulls the top 25 by
  cosine, applies a keyword-overlap re-rank and a dynamic threshold, then uses the
  returned fields directly (no Postgres round-trip for the excerpt + timestamp).
- **Episode view / search-in-episode** queries Pinecone with an `episode_id` metadata
  filter, and reads the full raw transcript from `podcast_transcripts` when needed.
- **Catalog** (`list_episodes`, library grouping) is served from `podcast_episodes`
  via the cached reader.
- **Re-index** (`transcribe.py --reindex`) rebuilds Pinecone from the saved Postgres
  transcripts — no re-transcription — e.g. after changing the embedding model.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design rationale.
