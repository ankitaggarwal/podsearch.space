# MCP Server Guide

## Overview

`mcp_tools/mcp_server.py` exposes 6 tools via the Model Context Protocol. Any MCP client (Claude Code, Claude Desktop, Cursor) can search podcasts and read transcripts.

**Transport:** stdio (stdin/stdout JSON-RPC) or SSE (when mounted in FastAPI)
**Auth:** Cloud endpoint requires `X-API-Key` header
**Stack:** Pinecone with integrated embedding (`llama-text-embed-v2`, 768-dim — embeds documents and queries) + PostgreSQL (episode catalog & transcripts) + a hosted LLM for answer synthesis. Transcription runs on a self-hosted gateway.

## Configuration

### Cloud (SSE) — connect to the live deployment

Paste this into your MCP client config (Claude Code, Claude Desktop, Cursor). Replace `YOUR_API_KEY` with the key you received:

```json
{
  "mcpServers": {
    "podcast-search": {
      "url": "https://your-deployment.example.com/mcp/sse",
      "headers": {
        "X-API-Key": "YOUR_API_KEY"
      }
    }
  }
}
```

### Local (stdio) — run against your own instance

Add this to your MCP client config, replacing the path with your local clone:

```json
{
  "mcpServers": {
    "podcast-search": {
      "command": "python3",
      "args": ["/full/path/to/mcp_tools/mcp_server.py"]
    }
  }
}
```

## Tools

### 1. `search_podcasts(query, top_k=10)`

Semantic search across all podcast transcripts. Returns relevant chunks with episode titles, timestamps, and relevance scores.

### 2. `list_episodes()`

Lists all indexed episodes with titles and podcast names.

### 3. `get_episode_transcript(episode_title)`

Returns the full transcript of a specific episode with timestamps. Supports partial title matching.

### 4. `search_in_episode(episode_title, query, top_k=5)`

Searches within a single episode. Same as `search_podcasts` but scoped to one episode via a Pinecone metadata filter.

### 5. `find_related_episodes(query)`

Groups search results by episode and ranks by total relevance score. Shows which episodes discuss a topic the most.

### 6. `get_library_stats()`

Returns total episodes, total chunks, and per-episode details (chunk counts, timestamps).


## Architecture

```
LLM (Claude, GPT, etc.)
  |
  v
MCP Client (Claude Code / Desktop / Cursor)
  | stdio or SSE
  v
mcp_tools/mcp_server.py (FastMCP)
  |-- Pinecone (integrated embedding + vector search; llama-text-embed-v2, 768d)
  +-- PostgreSQL (episode catalog & transcripts)
```

The MCP server returns raw context — it never calls any LLM. The calling LLM reads the context and writes its own answer.

## Adding New Tools

```python
@mcp.tool()
def my_tool(param: str) -> str:
    """Description the LLM sees to decide when to use this tool."""
    return "result as string"
```

No registration needed. FastMCP auto-discovers `@mcp.tool()` functions on startup.
