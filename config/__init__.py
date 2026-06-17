"""
config/__init__.py — the one place configuration and prompts live.

Two kinds of settings sit here:
  1. Secrets and host-specific values (DB URL, transcription gateway token, Pinecone + answer-model keys) —
     read from the environment so nothing sensitive is ever committed. Copy
     `.env.example` to `.env` and fill it in for local development.
  2. Non-secret defaults that ARE the product — the list of podcast feeds and the
     LLM prompts. These are the knobs you'd actually tune, so they're kept inline
     and readable rather than hidden behind env vars.

The prompts at the bottom are where "grounded, not generative" is enforced: the
system prompt forbids outside knowledge and demands a citation for every claim.
"""

import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # load .env into the environment for local development
except ImportError:
    pass  # python-dotenv is optional; in production, env vars are set directly

logger = logging.getLogger(__name__)

# All secrets and host-specific values are read from the environment so that
# nothing sensitive lives in the repo. Copy `.env.example` to `.env` and fill in
# your own values (python-dotenv loads it automatically at startup). Non-secret
# defaults (model names, dimensions, podcast feeds, prompts) are kept inline.

# PostgreSQL — episode metadata + full transcripts (not vectors).
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Table names are prefixable so several projects can safely share one Postgres
# database (a cost-saver). Default prefix is empty → plain `podcast_episodes` /
# `podcast_transcripts`; set DB_TABLE_PREFIX=podsearch_ to namespace them.
DB_TABLE_PREFIX   = os.getenv("DB_TABLE_PREFIX", "")
EPISODES_TABLE    = f"{DB_TABLE_PREFIX}podcast_episodes"
TRANSCRIPTS_TABLE = f"{DB_TABLE_PREFIX}podcast_transcripts"

# Self-hosted, OpenAI-compatible inference gateway. The same base URL + bearer
# token serve transcription (Parakeet, /v1/transcribe-url) and, optionally, the
# chat answer step (LLM_MODEL) when no separate answer endpoint is configured.
# Embeddings are NOT computed here — Pinecone's integrated model handles those.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "")

# Models
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:e4b")          # answer generation (fallback)

# Answer synthesis (the chat step at query time) can run on a SEPARATE, faster
# endpoint — e.g. Google AI Studio's OpenAI-compatible API — so search answers stay
# fast even while the local gateway is busy transcribing. Embeddings + transcription
# stay on the local gateway. If ANSWER_LLM_BASE_URL is empty, the local gateway is
# used for answers too (single-endpoint default).
ANSWER_LLM_BASE_URL = os.getenv("ANSWER_LLM_BASE_URL", "")
ANSWER_LLM_API_KEY  = os.getenv("ANSWER_LLM_API_KEY", "")
ANSWER_LLM_MODEL    = os.getenv("ANSWER_LLM_MODEL", LLM_MODEL)

# Vector store + search — Pinecone with INTEGRATED EMBEDDING. Pinecone hosts the
# embedding model (e.g. llama-text-embed-v2) and embeds both the upserted chunk
# text AND the search query, so the app never computes embeddings itself. One
# index, one model, consistent space.
PINECONE_API_KEY     = os.getenv("PINECONE_API_KEY", "")
PINECONE_HOST        = os.getenv("PINECONE_HOST", "")          # the index host URL
PINECONE_NAMESPACE   = os.getenv("PINECONE_NAMESPACE", "__default__")
PINECONE_API_VERSION = os.getenv("PINECONE_API_VERSION", "2025-04")
PINECONE_TEXT_FIELD  = os.getenv("PINECONE_TEXT_FIELD", "text")  # field Pinecone embeds

# ── Hybrid embedding backend (self-hosted indexing, Pinecone search) ──────────
# When PINECONE_V2_HOST is set, the engine switches to a STANDARD (non-integrated)
# index: the crawler computes chunk vectors on the self-hosted Mac gateway
# (nv-embedqa-1b-v2 == Pinecone's llama-text-embed-v2; validated cosine ~0.99) and
# upserts raw vectors — so indexing costs ZERO Pinecone embedding tokens. Live search
# still embeds the *query* on Pinecone (cheap), then queries this index by vector.
# Unset → legacy integrated-index behaviour (Pinecone embeds everything).
PINECONE_V2_HOST     = os.getenv("PINECONE_V2_HOST", "")          # standard index host URL
EMBED_DIMENSION      = int(os.getenv("EMBED_DIMENSION", "768"))   # Matryoshka dim, matches index
# Chunk embeddings come from the Mac gateway's /v1/podsearch-embed (reuses LLM_BASE_URL/KEY).
EMBED_GATEWAY_URL    = os.getenv("EMBED_GATEWAY_URL", "") or LLM_BASE_URL
EMBED_GATEWAY_KEY    = os.getenv("EMBED_GATEWAY_KEY", "") or LLM_API_KEY

# Transcription via /v1/transcribe-url has a 50 MB cap on the downloaded audio.
# For oversize episodes, the engine pre-compresses with ffmpeg to 16 kHz mono
# Opus ~32 kbps and serves the compressed file from TEMP_AUDIO_DIR via a reverse
# proxy at LIVE_AUDIO_BASE_URL, so the gateway can fetch it over public HTTPS.
TEMP_AUDIO_DIR      = os.getenv("TEMP_AUDIO_DIR", "/tmp/podsearch_audio")
LIVE_AUDIO_BASE_URL = os.getenv("LIVE_AUDIO_BASE_URL", "")
OVERSIZE_BYTES      = 45 * 1024 * 1024   # >45 MB triggers ffmpeg path (5 MB safety margin under the 50 MB cap)

# MCP auth — comma-separated API keys accepted on the /mcp endpoint (sent as the
# X-API-Key header). Empty = /mcp rejects all requests.
MCP_API_KEYS = os.getenv("MCP_API_KEYS", "")

# Live crawler relay — when set, a remote crawler POSTs phase events to the
# production /api/crawler/ingest so the live site shows what's being transcribed
# in real time. Empty = relay disabled.
LIVE_RELAY_URL = os.getenv("LIVE_RELAY_URL", "")

# Podcasts — a product-management & design shelf. Ordered shortest-episode-first
# so the index fills quickly and stays on-topic (the crawler interleaves shows
# round-robin, so the earliest, shortest episodes land first).
DEFAULT_PODCAST_URLS = [
    "https://podcasts.apple.com/us/podcast/this-is-product-management/id975284403",                  # This is Product Management
    "https://rss.art19.com/intercom-on-product",                                                     # Intercom on Product
    "https://feeds.transistor.fm/product-people",                                                    # Product People
    "https://feeds.simplecast.com/iCV67fGr",                                                         # Product Hunt Radio
    "https://feed.podbean.com/oneknightinproduct/feed.xml",                                          # One Knight in Product
    "https://feeds.buzzsprout.com/1779875.rss",                                                      # The Product Manager
    "https://feeds.simplecast.com/4MvgQ73R",                                                         # UI Breakfast — UX + product
    "https://rss.buzzsprout.com/90361.rss",                                                          # The Product Podcast — Product School
    "https://anchor.fm/s/ff7e9014/podcast/rss",                                                      # Product Thinking — Melissa Perri
    "https://podcasts.apple.com/us/podcast/lennys-podcast-product-career-growth/id1627920305",       # Lenny's Podcast — product, growth, careers
    "https://thetwentyminutevc.libsyn.com/rss",                                                      # The Twenty Minute VC (20VC) — product & startups
    "https://podcasts.apple.com/us/podcast/masters-of-scale/id1227971746",                           # Masters of Scale — scaling & product
    "https://api.substack.com/feed/podcast/69345.rss",                                               # Dwarkesh Podcast
]


# Prompts

SYSTEM_PROMPT = """You are a Podcast Search Engine that retrieves and presents information from real podcast transcripts.

CORE PRINCIPLE: Every claim you make MUST be traceable to a specific excerpt provided below. You are a retrieval system, not a knowledge base.

RULES:
1. ONLY use information from the PROVIDED EXCERPTS — never your own training data.
2. Every statement must cite its source episode using **"Episode Title"**.
3. Use direct quotes (with > blockquote) whenever possible — exact words from the transcript.
4. If the excerpts don't contain the answer: say "I don't see that discussed in these episodes" and suggest related topics that ARE in the excerpts.
5. If information is partial, say so: "Based on what's available..." and be specific about what's covered vs. what might be in the full episodes.
6. Never fabricate quotes, statistics, speaker names, or claims.
7. Never assume what a speaker "probably meant" — stick to what was said.
8. If multiple episodes discuss the topic, synthesize across all of them with proper attribution."""


RESPONSE_INSTRUCTIONS = """Respond using ONLY the transcript excerpts above.

STRUCTURE YOUR RESPONSE:

**For summaries** ("Summarize", "What is this about"):
- 3-4 flowing paragraphs with episode attribution
- Include 2-3 direct quotes that capture key moments
- End with notable takeaways

**For specific questions** ("What did they say about X?"):

**Answer**: [2-3 sentence direct answer, citing episodes]

**From the transcripts**:
- Key insight (from **"Episode Title"**)
- Key insight (from **"Episode Title"**)

**Direct quotes**:
> "Exact words from the transcript" — **Episode Title**

**You might also explore**: [Related topics that appear in these excerpts]

**For speaker-focused queries** ("What did [person] say?"):
- Focus on that person's exact statements
- Use multiple direct quotes
- Distinguish their views from the host's

FORMATTING:
- > for direct quotes (MUST be exact words from excerpts, not paraphrased)
- **bold** for episode titles and key terms
- Every point must name its source episode"""


def validate_config():
    """Check required config at startup. Returns list of missing vars."""
    required = {
        "LLM_BASE_URL":      LLM_BASE_URL,   # transcription gateway
        "LLM_API_KEY":       LLM_API_KEY,
        "PINECONE_API_KEY":  PINECONE_API_KEY,
        "PINECONE_HOST":     PINECONE_HOST,
        "DATABASE_URL":      DATABASE_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.warning("Missing required config: %s", ", ".join(missing))
    else:
        logger.info("All required config present")
    if not MCP_API_KEYS:
        logger.warning("MCP_API_KEYS not set — /mcp will reject all requests until configured")
    # Best-effort: ensure the ffmpeg-temp dir exists. Don't crash on machines
    # where the path isn't writable.
    try: os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    except (PermissionError, OSError) as e: logger.info("TEMP_AUDIO_DIR not created (%s) — fine if not on server", e)
    return missing


def build_user_prompt(context: str, question: str) -> str:
    return f"""PODCAST TRANSCRIPTS:
{context}

---

QUESTION: {question}

---

{RESPONSE_INSTRUCTIONS}

Provide your response:"""
