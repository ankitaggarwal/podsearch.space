"""
mcp_server.py — the agent-facing surface.

MCP (the Model Context Protocol) is a standard way to give an AI agent — Claude Code,
Claude Desktop, Cursor — a set of callable "tools." This file exposes the SAME podcast
search engine as six such tools, so an agent can query the library directly instead of
going through the web UI.

The key design rule: **these tools return raw context, never a finished answer.** They
do semantic search and hand back the matching transcript excerpts (with episode titles,
timestamps, scores). They never call an LLM themselves — the *calling* agent reads the
excerpts and does its own grounded reasoning. That keeps this server cheap, fast, and
composable, and leaves the "writing" to whoever called it.

The engine is imported lazily (inside the functions) so this file can be launched two
ways from the same code:
    - stdio  — run this file directly; an MCP client spawns it as a subprocess
    - SSE    — mounted at /mcp by app.py, sharing the web server's engine singleton

Run standalone:  python3 mcp_tools/mcp_server.py
"""

import os
import sys
from pathlib import Path
# This file can run as a standalone script (stdio transport), so make the project
# root importable regardless of the working directory it's launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from qdrant_client.models import Filter, FieldCondition, MatchValue
from config import EMBEDDING_MODEL

# Hosts allowed to reach the SSE endpoint (DNS-rebinding protection). Add your
# deployment domain via MCP_ALLOWED_HOSTS (comma-separated); localhost is always
# allowed.
_allowed_hosts = ["localhost:*", "127.0.0.1:*"]
_allowed_hosts += [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

mcp = FastMCP(
    "podcast-search",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
    ),
)


def _get_engine():
    from transcribe import get_engine
    return get_engine()


def _match_episode(title_query):
    episodes = _get_engine().get_indexed_episodes()
    query_lower = title_query.lower()
    return [
        (eid, ep["title"], ep["podcast_title"])
        for eid, ep in episodes.items()
        if query_lower in ep["title"].lower()
    ]


def _strip_episode_prefix(text):
    if text.startswith("Episode:"):
        return text.split("\n\n", 1)[-1]
    return text


def _embed_query(engine, query):
    """Embed a query string via the shared inference gateway (embeddinggemma).
    Returns (embedding, None) on success, (None, error_message) on failure —
    tools surface a graceful error instead of crashing."""
    try:
        resp = engine.llm_client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
        return resp.data[0].embedding, None
    except Exception as e:
        return None, f"Embedding service unavailable: {str(e)[:120]}"


def _safe_query(engine, query_vector, limit=10, query_filter=None):
    """Wrap qdrant.query_points() so a vector-DB outage surfaces as a user-readable
    message instead of an unhandled exception. Returns (list[ScoredPoint], None)
    on success, (None, error_message) on failure."""
    try:
        response = engine.qdrant.query_points(
            collection_name=engine.collection,
            query=query_vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        return response.points, None
    except Exception as e:
        return None, f"Search service unavailable: {str(e)[:120]}"


# Each @mcp.tool() registers a function as a tool the agent can call. FastMCP turns
# the type hints into the tool's input schema, and — importantly — the docstring is
# what the agent reads to decide WHEN to use the tool. So these docstrings are written
# for the model, not just for humans: they say what the tool is for and what it returns.
@mcp.tool()
def search_podcasts(query: str, top_k: int = 10) -> str:
    """Search across all podcast transcripts for answers to a question. Use this for any product, startup, or business question — it finds the most relevant transcript excerpts with episode titles, timestamps, and relevance scores. Returns raw context for you to synthesize an answer from."""
    engine = _get_engine()

    query_embedding, err = _embed_query(engine, query)
    if err:
        return err

    results, err = _safe_query(engine, query_embedding, limit=top_k)
    if err:
        return err

    if not results or results[0].score < 0.25:
        return "No relevant content found for this query."

    parts = []
    for i, match in enumerate(results, 1):
        if match.score < 0.20:
            continue
        meta = match.payload or {}
        episode = meta.get("episode_title", "Unknown")
        text = meta.get("text", "")
        start_time = meta.get("start_time")
        end_time = meta.get("end_time")
        speakers = meta.get("speakers", [])
        relevance = "HIGH" if match.score >= 0.5 else "MEDIUM" if match.score >= 0.35 else "LOW"

        time_str = ""
        if start_time is not None and end_time is not None:
            s_min, s_sec = int(start_time // 60), int(start_time % 60)
            e_min, e_sec = int(end_time // 60), int(end_time % 60)
            time_str = f"\nTimestamp: {s_min}:{s_sec:02d} - {e_min}:{e_sec:02d}"

        speaker_str = ""
        if speakers:
            speaker_str = f"\nSpeakers: {', '.join(str(s) for s in speakers)}"

        parts.append(
            f"[{i}] (Relevance: {relevance}, Score: {match.score:.3f})\n"
            f'Episode: "{episode}"{time_str}{speaker_str}\n'
            f"Content: {text}"
        )

    return "\n\n---\n\n".join(parts) if parts else "No relevant content found for this query."


@mcp.tool()
def list_episodes() -> str:
    """List all available podcast episodes with titles and podcast names. Use this when the user asks what episodes or podcasts are available, or when you need an episode title for get_episode_transcript or search_in_episode."""
    episodes = _get_engine().get_indexed_episodes()
    if not episodes:
        return "No episodes indexed yet."

    lines = []
    for i, ep in enumerate(episodes.values(), 1):
        lines.append(f"{i}. {ep['title']} ({ep['podcast_title']})")
    return "\n".join(lines)


@mcp.tool()
def get_episode_transcript(episode_title: str) -> str:
    """Get the full transcript of a specific episode with timestamps. Use this when the user wants a summary of an episode, wants to read what was said, or asks about a specific episode by name. Supports partial title matching — you don't need the exact title."""
    matches = _match_episode(episode_title)
    if not matches:
        return f"No episode found matching '{episode_title}'. Use list_episodes() to see available episodes."
    if len(matches) > 1:
        titles = "\n".join(f"  - {t}" for _, t, _ in matches)
        return f"Multiple episodes match '{episode_title}':\n{titles}\nPlease be more specific."

    episode_id, title, _ = matches[0]

    segments = _get_engine().get_transcript_segments(episode_id)
    if not segments:
        return f"No transcript found for '{title}'."

    lines = [f"# {title}\n"]
    for seg in segments:
        start = seg.get("start", 0)
        speaker = seg.get("speaker_name") or seg.get("speaker")
        text = seg.get("text", "").strip()
        timestamp = f"[{int(start // 60)}:{int(start % 60):02d}]"
        speaker_str = f" {speaker}:" if speaker is not None else ""
        lines.append(f"{timestamp}{speaker_str} {text}")
    return "\n".join(lines)


@mcp.tool()
def search_in_episode(episode_title: str, query: str, top_k: int = 5) -> str:
    """Search within a single episode for a specific topic or question. Use this when the user already mentioned an episode and wants to find something specific in it — like "what did they say about pricing in the Stripe episode?"."""
    matches = _match_episode(episode_title)
    if not matches:
        return f"No episode found matching '{episode_title}'. Use list_episodes() to see available episodes."
    if len(matches) > 1:
        titles = "\n".join(f"  - {t}" for _, t, _ in matches)
        return f"Multiple episodes match '{episode_title}':\n{titles}\nPlease be more specific."

    episode_id, title, _ = matches[0]
    engine = _get_engine()

    query_embedding, err = _embed_query(engine, query)
    if err:
        return err

    results, err = _safe_query(
        engine,
        query_embedding,
        limit=top_k,
        query_filter=Filter(must=[FieldCondition(
            key="episode_id", match=MatchValue(value=episode_id)
        )]),
    )
    if err:
        return err

    if not results:
        return f"No relevant content found for '{query}' in '{title}'."

    parts = [f"Results from: **{title}**\n"]
    for i, match in enumerate(results, 1):
        meta = match.payload or {}
        text = meta.get("text", "")
        start_time = meta.get("start_time")
        end_time = meta.get("end_time")
        speakers = meta.get("speakers", [])
        relevance = "HIGH" if match.score >= 0.5 else "MEDIUM" if match.score >= 0.35 else "LOW"

        time_str = ""
        if start_time is not None and end_time is not None:
            s_min, s_sec = int(start_time // 60), int(start_time % 60)
            e_min, e_sec = int(end_time // 60), int(end_time % 60)
            time_str = f" | {s_min}:{s_sec:02d}-{e_min}:{e_sec:02d}"

        speaker_str = ""
        if speakers:
            speaker_str = f" | Speakers: {', '.join(str(s) for s in speakers)}"

        parts.append(f"[{i}] (Relevance: {relevance}{time_str}{speaker_str})\n{text}")

    return "\n\n---\n\n".join(parts)


@mcp.tool()
def find_related_episodes(query: str) -> str:
    """Find which episodes discuss a topic, ranked by how much they cover it. Use this when the user asks "which episodes talk about X?" or wants recommendations — unlike search_podcasts which returns excerpts, this returns a ranked list of episodes."""
    engine = _get_engine()

    query_embedding, err = _embed_query(engine, query)
    if err:
        return err

    # Server-side group-by — Qdrant returns top-K episodes with their best
    # chunks already grouped, replacing 30 lines of Python aggregation. Episodes
    # are ranked by their single best matching chunk (cleaner "relevance" signal
    # than sum-of-scores, which biased toward long episodes with many mid hits).
    try:
        response = engine.qdrant.query_points_groups(
            collection_name=engine.collection,
            query=query_embedding,
            group_by="episode_id",
            limit=15,
            group_size=5,
            score_threshold=0.20,
            with_payload=True,
        )
    except Exception as e:
        return f"Search service unavailable: {str(e)[:120]}"

    groups = response.groups
    if not groups or groups[0].hits[0].score < 0.25:
        return f"No episodes found related to '{query}'."

    parts = [f'Episodes related to: "{query}"\n']
    for i, group in enumerate(groups, 1):
        top_hit = group.hits[0]
        meta = top_hit.payload or {}
        title = meta.get("episode_title", "Unknown")
        best_score = top_hit.score
        text = _strip_episode_prefix(meta.get("text", ""))
        excerpt = text[:300] + "..." if len(text) > 300 else text
        relevance = "HIGH" if best_score >= 0.5 else "MEDIUM" if best_score >= 0.35 else "LOW"
        parts.append(
            f"{i}. **{title}**\n"
            f"   Relevance: {relevance} | {len(group.hits)} matching sections\n"
            f"   Preview: {excerpt}"
        )

    return "\n\n".join(parts)


@mcp.tool()
def get_library_stats() -> str:
    """Get library statistics — total episodes, total podcasts, and per-podcast episode counts. Use this when the user asks how many episodes, how many podcasts, or wants an overview of the library."""
    engine = _get_engine()
    episodes = engine.get_indexed_episodes()

    if not episodes:
        return "The podcast library is empty."

    total_episodes = len(episodes)
    try:
        with engine._pg_cur() as cur:
            cur.execute("SELECT COALESCE(SUM(chunk_count), 0) FROM podcast_episodes")
            total_chunks = cur.fetchone()[0]
    except Exception:
        total_chunks = 0

    by_podcast = {}
    for ep in episodes.values():
        by_podcast.setdefault(ep["podcast_title"], []).append(ep["title"])

    lines = [
        "# Podcast Library Stats\n",
        f"- **Total episodes indexed:** {total_episodes}",
        f"- **Total searchable chunks:** {total_chunks}",
        f"- **Avg chunks per episode:** {total_chunks // max(total_episodes, 1)}",
        f"- **Podcasts:** {len(by_podcast)}\n",
        "## Podcasts\n",
    ]

    for podcast, titles in sorted(by_podcast.items()):
        lines.append(f"- **{podcast}**: {len(titles)} episodes")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
