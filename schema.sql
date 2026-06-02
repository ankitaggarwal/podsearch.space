-- Podcast Search — PostgreSQL schema.
--
-- The app makes no tables of its own; create them once on first deploy:
--   psql "$DATABASE_URL" -f schema.sql
--
-- If you set DB_TABLE_PREFIX (to share one database across projects), create the
-- tables with that prefix to match — e.g.:
--   sed 's/podcast_/podsearch_podcast_/g' schema.sql | psql "$DATABASE_URL"

CREATE TABLE IF NOT EXISTS podcast_episodes (
    id            TEXT PRIMARY KEY,        -- md5(podcast_title + "_" + episode_title)
    title         TEXT NOT NULL,
    podcast_title TEXT NOT NULL,
    audio_url     TEXT,
    image_url     TEXT,
    chunk_count   INTEGER     DEFAULT 0,
    indexed_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS podcast_transcripts (
    episode_id TEXT PRIMARY KEY REFERENCES podcast_episodes(id) ON DELETE CASCADE,
    segments   JSONB NOT NULL,            -- array of {text, start_time, end_time, speakers}
    created_at TIMESTAMPTZ DEFAULT NOW()
);
