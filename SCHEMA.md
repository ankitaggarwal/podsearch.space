# Data model

Podcast Search keeps facts in **PostgreSQL** and similarity in **Qdrant**. Postgres
is the source of truth for the catalog and the raw transcripts; Qdrant holds the
chunk vectors used for retrieval. Every vector carries enough metadata to trace a
match back to the exact episode and moment.

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

## Qdrant

A single collection of chunk vectors.

| Property | Value |
|----------|-------|
| Collection | `podcast-search` (configurable via `QDRANT_COLLECTION`) |
| Vector size | **768** (`embeddinggemma`) |
| Distance | **Cosine** |
| Point ID | deterministic UUIDv5 of `<episode_id>_c<NNNN>` — re-indexing overwrites cleanly |

### Payload (per chunk)

| Key | Type | Used for |
|-----|------|----------|
| `episode_id` | `str` | filter to a single episode; join back to Postgres |
| `episode_title` | `str` | display + keyword re-rank |
| `podcast_title` | `str` | display |
| `text` | `str` | the chunk text (the excerpt shown / sent to the LLM) |
| `chunk_index` | `int` | order within the episode |
| `start_time` | `float` | seconds — the "jump to the moment" timestamp |
| `end_time` | `float` | seconds |
| `speakers` | `list` | reserved (empty — see above) |

### How the two stores work together

- **Search** embeds the query, pulls the top 25 from Qdrant by cosine, applies a
  keyword-overlap re-rank and a dynamic threshold, then uses the surviving payloads
  directly (no Postgres round-trip needed for the excerpt + timestamp).
- **Episode view** filters Qdrant by `episode_id` (both `scroll` to enumerate all
  chunks and `search` to rank the most relevant), and reads the full raw transcript
  from `podcast_transcripts` when needed.
- **Catalog** (`list_episodes`, library grouping) is served from `podcast_episodes`
  via the cached reader.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design rationale.
