# Podcast Search — a case study

A retrieval system that turns a shelf of podcasts into a knowledge base you can
ask in plain English — and get back an answer that **cites the exact episode and
timestamp**. This is the written companion to the [slide deck](index.html); it's
the "why" behind the design.

---

## The problem

Podcasts are one of the richest sources of operator knowledge we have, and one
of the worst to *retrieve* from. The thing you want — how someone thinks about
pricing, hiring, a specific tool — is buried at minute 47 of a two-hour episode,
unsearchable and unquotable.

A naive "ask an LLM about podcasts" tool makes it *worse*: it produces fluent,
confident answers you have no way to verify. For knowledge you intend to act on,
an unverifiable answer is no better than a guess.

So the brief I set myself: **make podcast knowledge searchable, and make every
answer checkable.**

## The approach

One linear pipeline, framed as three transformations:

```
sound → text              text → searchable meaning            meaning → a cited answer
(RSS → Parakeet)          (chunk → Pinecone embed+store → retrieve)  (grounded synthesis)
```

1. **Ingest** — read RSS feeds (26 shows wired in), resolve each episode's audio
   URL (including via the iTunes lookup API for Apple Podcasts links),
   round-robin across shows so no single feed dominates, and dedupe on audio URL.
2. **Transcribe** — Parakeet speech-to-text via a self-hosted inference gateway,
   with utterance timestamps: *what was said, and when.*
3. **Chunk** — group the transcript into ~1,000-character passages, carrying the
   timestamps forward on every chunk.
4. **Store** — each chunk is upserted to Pinecone as a text record; Pinecone's
   integrated embedding model (`llama-text-embed-v2`) vectorizes it on the way in,
   with the metadata attached.
5. **Retrieve** — Pinecone embeds the question, pulls the top 25 by similarity,
   then a keyword re-rank and a dynamic threshold trim to the keepers.
6. **Answer** — Gemma 4 synthesizes an answer **only** from the retrieved
   excerpts, under a system prompt that forbids outside knowledge and requires a
   citation for every claim.

## Three decisions worth calling out

**1. Grounded, not generative — the whole point.**
The LLM is treated as a *synthesizer*, not a knowledge base. The system prompt's
first rule: "Every claim you make MUST be traceable to a specific excerpt." If
the excerpts don't contain the answer, the model is instructed to say so rather
than invent one. The retrieval layer carries timestamps all the way through, so
the final answer can render as a **receipt** — each source links back to the
exact moment in the episode. That verifiability *is* the product.

**2. Hybrid retrieval — cheap, and it fixes a real failure mode.**
Pure vector search understands meaning but underweights exact tokens — a guest's
name, a product, a number. A passage that literally says "RevenueCat" can lose to
one that's merely *about* pricing. So after the vector pull I add a lightweight
keyword-overlap boost (capped at `+0.15`, after stop-word removal) and keep
results within 55% of the best combined score (floor `0.20`). It recovers proper
nouns and exact phrases for almost none of the cost of a full cross-encoder
re-ranker — a deliberate 80/20 call.

**3. One engine, two surfaces.**
The same retrieval engine backs both the human web app and an **MCP server**
(6 tools) that Claude Code / Desktop / Cursor can call directly. The MCP server
returns *raw context only* — it never calls an LLM; the calling agent does its
own grounded reasoning. A process-wide singleton shares caches and clients
between the two surfaces, so the capability is built once and exposed twice.

## The right tool for each stage

Each stage runs where it's cheapest and most reliable, wired together by config:
**transcription** stays on a **self-hosted Parakeet gateway** (the heavy, high-volume
audio work — no per-minute API bill); **embeddings + vector search** are handed to
**Pinecone's integrated embedding** (it embeds the chunk on upsert and the query on
search, so there's no embedding service to run, and search never competes with
indexing); and **answer generation** runs on a fast hosted model. The whole thing is
endpoint-agnostic — every stage is a couple of environment variables. The models are
commodities; the retrieval quality is the moat.

## Engineering for the real world

- **Caching:** the episode catalog is read constantly and changes rarely, so it's
  served from an in-memory cache backed by PostgreSQL and refreshed on a 2-minute
  TTL — stale-while-revalidate, so reads never block on the database.
- **Resilience:** auth + rate limiting (`X-API-Key`) on the sensitive endpoints;
  skip a show after 3 consecutive failures; audio-URL dedup catches title-change
  "phantom" duplicates; oversize episodes (>45 MB) are pre-compressed with ffmpeg
  to 16 kHz mono Opus so they fit the transcription gateway's 50 MB cap; every
  failure is logged with a reason and streamed to the UI.
- **Deployment:** Docker on a small always-on VPS (the heavy lifting lives behind
  the inference gateway), health-checked, auto-deployed from `main`.

## Outcome & what I'd do next

The result is a system where **a two-hour episode collapses into a handful of
cited sentences you can trust** — and where the same library is queryable by both
people and agents. The retrieval quality, not the model, is what makes it useful;
the model is the garnish.

Next steps I'd prioritize: a true cross-encoder re-rank stage (measured against
the keyword-boost baseline), speaker diarization for per-speaker attribution, and
evals on a labelled question set to tune the threshold empirically rather than by
feel.

---

*Stack: FastAPI · React · self-hosted Parakeet (transcription) · Pinecone with
integrated `llama-text-embed-v2` (embeddings + vector search) · a hosted LLM for
answers · PostgreSQL · Docker. Source: the companion code repo · MIT.*
