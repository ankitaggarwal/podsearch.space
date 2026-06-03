# Podcast Search

Search hours of podcast content in seconds. Ask a question in plain English and get
an answer **grounded in real transcripts** — with citations and timestamps, so every
claim is traceable back to the episode it came from.

🌐 **Live:** [podsearch.space](https://podsearch.space)

![Podcast Search — grounded answers from real podcast transcripts](docs/hero.gif)

It ingests podcast RSS feeds, transcribes the audio with a self-hosted inference
gateway, chunks the transcripts and stores them in Pinecone (whose integrated model
embeds them), and serves a conversational search interface that answers **only** from
what was actually said.

## How it works

```
RSS feed → audio URL → transcribe → chunk → Pinecone (embed + store) → search → grounded answer
                                                        │
                                          one engine, two surfaces
                                                        │
                                              web app  ·  MCP server
```

1. **Ingest** — read RSS feeds (26 shows wired in), resolve each episode's real audio
   URL (including via the iTunes lookup API for Apple Podcasts links), interleave
   round-robin across shows, and dedupe on audio URL.
2. **Transcribe** — send the audio to a self-hosted gateway (Parakeet) for per-utterance
   text with timestamps; oversize episodes are compressed with ffmpeg first.
3. **Chunk** — group the transcript into ~1,000-character passages (timestamps carried
   forward).
4. **Store** — upsert the chunks to Pinecone as text records; its integrated model
   (`llama-text-embed-v2`) embeds them on the way in. Raw transcripts go to PostgreSQL
   as the safety net.
5. **Retrieve & answer** — Pinecone embeds the question, pulls the top 25, re-rank with a keyword
   boost, trim with a dynamic threshold, and let Gemma 4 synthesize an answer **only**
   from the retrieved excerpts — citing every claim.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design rationale and [SCHEMA.md](SCHEMA.md)
for the data model. The retrieval engine is also exposed as an
[MCP server](docs/MCP_GUIDE.md) so any AI agent (Claude Code, Claude Desktop, Cursor)
can search the library directly.

## Design principle: grounded, not generative

A naive "ask an LLM about podcasts" tool produces fluent, confident answers you have no
way to verify. This system treats the LLM as a **synthesizer, not a knowledge base**:
the answer is built only from retrieved excerpts, every claim must cite its source
episode, and if the excerpts don't contain the answer, the model says so. Timestamps are
carried all the way through, so each answer renders as a **receipt** you can check.

## Stack

| Layer | Technology |
|-------|-----------|
| Web server | FastAPI (Python) |
| Frontend | React (single-file SPA, no build step) |
| Transcription | Parakeet via a self-hosted, OpenAI-compatible inference gateway (async) |
| Episode catalog + transcripts | PostgreSQL |
| Embeddings + vector search | **Pinecone** with integrated embedding (`llama-text-embed-v2`, 768-dim, cosine) |
| Answer synthesis | a hosted LLM (OpenAI-compatible, e.g. Google AI Studio) |
| Deployment | Docker + Caddy on a small VPS |

> **Pinecone integrated embedding** means Pinecone hosts the embedding model and embeds
> both the upserted chunk text and the search query — the app never computes vectors, and
> search never competes with indexing. The self-hosted gateway is used only for
> transcription. Each stage is a couple of environment variables.

> **The inference gateway** is a self-hosted, OpenAI-compatible endpoint for
> transcription (and, optionally, the answer step) behind one base URL and one token. The app
> talks to it with the `openai` Python SDK — no managed OpenAI service is contacted.
> Swap in any OpenAI-compatible endpoint by changing `LLM_BASE_URL` and `LLM_API_KEY`.

## Layout

```
app.py              FastAPI server: REST API + MCP mount + auth + live relay
transcribe.py       core engine: RSS parsing, transcription, chunking, embedding, search
config/             configuration + prompts (all secrets from the environment)
mcp_tools/
  mcp_server.py     MCP server — 6 tools for AI agents
ui/
  index.html        React SPA (search, library, episode pages, live crawler panel)
schema.sql          PostgreSQL tables (catalog + transcripts)
Dockerfile          Python 3.11-slim + ffmpeg
docker-compose.yml  web + pipeline + caddy
docs/MCP_GUIDE.md   MCP server documentation
```

## Running locally

Requires Python 3.11+ and `ffmpeg` on `PATH` (for episodes over 50 MB; on macOS:
`brew install ffmpeg`).

```bash
git clone https://github.com/ankitaggarwal/podsearch.space.git
cd podsearch.space
pip3 install -r requirements.txt

cp .env.example .env        # then fill in your values
python3 app.py              # API + frontend + /mcp on http://localhost:8000
```

Index episodes:

```bash
python3 transcribe.py --all          # every configured feed
python3 transcribe.py --episodes 5   # up to 5 per feed
```

## Configuration

All configuration comes from the environment; see [`.env.example`](.env.example) for the
full list (`DATABASE_URL`, `LLM_BASE_URL/KEY/MODEL` for transcription,
`PINECONE_API_KEY/HOST` for embedding + search, `ANSWER_LLM_BASE_URL/KEY/MODEL`, `MCP_API_KEYS`, `MCP_ALLOWED_HOSTS`). Nothing sensitive is
committed to the repo. The `/mcp` endpoint rejects all requests unless `MCP_API_KEYS` is
set.

## Deployment

Docker Compose (web + pipeline + caddy) on a single droplet, with Postgres, Pinecone, and
the gateway hosted elsewhere. See [DEPLOY.md](DEPLOY.md).

## Case study

A written case study and an animated slide deck walk through the problem, the pipeline,
and the engineering decisions worth calling out — see the companion deck.

## License

[MIT](LICENSE) © Ankit Aggarwal
