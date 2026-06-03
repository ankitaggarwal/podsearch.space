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


def _search(engine, query, top_k=10, episode_id=None):
    """Run a Pinecone text search via the engine (Pinecone embeds the query).
    Returns (matches, None) on success or (None, error_message) on failure, so a
    backend outage surfaces as a readable message instead of an exception. Each
    match is a dict: {id, score, text, episode_id, episode_title, podcast_title, ...}."""
    try:
        return engine._pc_search(query, top_k=top_k, episode_id=episode_id), None
    except Exception as e:
        return None, f"Search service unavailable: {str(e)[:120]}"


def _relevance(score):
    # The integrated embedding model's scores run lower than raw cosine.
    return "HIGH" if score >= 0.35 else "MEDIUM" if score >= 0.20 else "LOW"


# Each @mcp.tool() registers a function as a tool the agent can call. FastMCP turns
# the type hints into the tool's input schema, and — importantly — the docstring is
# what the agent reads to decide WHEN to use the tool. So these docstrings are written
# for the model, not just for humans: they say what the tool is for and what it returns.
@mcp.tool()
def search_podcasts(query: str, top_k: int = 10) -> str:
    """Search across all podcast transcripts for answers to a question. Use this for any product, startup, or business question — it finds the most relevant transcript excerpts with episode titles, timestamps, and relevance scores. Returns raw context for you to synthesize an answer from."""
    engine = _get_engine()
    results, err = _search(engine, query, top_k=top_k)
    if err:
        return err
    if not results or results[0]["score"] < 0.08:
        return "No relevant content found for this query."

    parts = []
    for i, m in enumerate(results, 1):
        episode = m.get("episode_title", "Unknown")
        text = m.get("text", "")
        start_time, end_time = m.get("start_time"), m.get("end_time")
        time_str = ""
        if start_time is not None and end_time is not None:
            s_min, s_sec = int(float(start_time) // 60), int(float(start_time) % 60)
            e_min, e_sec = int(float(end_time) // 60), int(float(end_time) % 60)
            time_str = f"\nTimestamp: {s_min}:{s_sec:02d} - {e_min}:{e_sec:02d}"
        parts.append(
            f"[{i}] (Relevance: {_relevance(m['score'])}, Score: {m['score']:.3f})\n"
            f'Episode: "{episode}"{time_str}\n'
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
    results, err = _search(engine, query, top_k=top_k, episode_id=episode_id)
    if err:
        return err
    if not results:
        return f"No relevant content found for '{query}' in '{title}'."

    parts = [f"Results from: **{title}**\n"]
    for i, m in enumerate(results, 1):
        text = m.get("text", "")
        start_time, end_time = m.get("start_time"), m.get("end_time")
        time_str = ""
        if start_time is not None and end_time is not None:
            s_min, s_sec = int(float(start_time) // 60), int(float(start_time) % 60)
            e_min, e_sec = int(float(end_time) // 60), int(float(end_time) % 60)
            time_str = f" | {s_min}:{s_sec:02d}-{e_min}:{e_sec:02d}"
        parts.append(f"[{i}] (Relevance: {_relevance(m['score'])}{time_str})\n{text}")
    return "\n\n---\n\n".join(parts)


@mcp.tool()
def find_related_episodes(query: str) -> str:
    """Find which episodes discuss a topic, ranked by how much they cover it. Use this when the user asks "which episodes talk about X?" or wants recommendations — unlike search_podcasts which returns excerpts, this returns a ranked list of episodes."""
    engine = _get_engine()
    # Pull a wide set, then group by episode in Python — rank each episode by its
    # single best-matching chunk (a cleaner signal than sum-of-scores, which biases
    # toward long episodes with many mid hits).
    results, err = _search(engine, query, top_k=40)
    if err:
        return err
    if not results or results[0]["score"] < 0.1:
        return f"No episodes found related to '{query}'."

    by_ep = {}  # episode_id -> {title, best, count, best_text}
    for m in results:
        eid = m.get("episode_id", "")
        g = by_ep.setdefault(eid, {"title": m.get("episode_title", "Unknown"),
                                   "best": 0, "count": 0, "best_text": ""})
        g["count"] += 1
        if m["score"] > g["best"]:
            g["best"], g["best_text"] = m["score"], m.get("text", "")
    ranked = sorted(by_ep.values(), key=lambda g: g["best"], reverse=True)[:15]

    parts = [f'Episodes related to: "{query}"\n']
    for i, g in enumerate(ranked, 1):
        excerpt = g["best_text"][:300] + "..." if len(g["best_text"]) > 300 else g["best_text"]
        parts.append(
            f"{i}. **{g['title']}**\n"
            f"   Relevance: {_relevance(g['best'])} | {g['count']} matching sections\n"
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
        from config import EPISODES_TABLE
        with engine._pg_cur() as cur:
            cur.execute(f"SELECT COALESCE(SUM(chunk_count), 0) FROM {EPISODES_TABLE}")
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
