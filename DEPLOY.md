# Deploying Podcast Search (DigitalOcean)

A simple Docker-based deploy you can reuse for other apps. Everything runs in
three small containers on one droplet:

| Container | What it does |
|-----------|--------------|
| `web`      | The FastAPI app — search API, the website, and `/mcp` |
| `pipeline` | The indexer, looping continuously (discover → transcribe → embed) |
| `caddy`    | Reverse proxy that gives the site free automatic HTTPS |

PostgreSQL, Qdrant, and the inference gateway live **elsewhere** and are configured
through `.env`. They are **not** run on the droplet.

---

## One-time server setup

SSH into the droplet (`ssh root@YOUR_SERVER_IP`), then:

```bash
# 1. Install Docker (official convenience script)
curl -fsSL https://get.docker.com | sh

# 2. Add 2 GB of swap (small droplets have little RAM; this prevents crashes)
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 3. Open the firewall for web traffic + SSH (if ufw is enabled)
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable

# 4. Get the code (public repo)
git clone https://github.com/ankitaggarwal/podsearch.space.git
cd podsearch.space
```

## Add the secrets (never committed to git)

Create `.env` on the server with your real values — see [`.env.example`](.env.example)
for the full list:

```bash
cp .env.example .env
nano .env
```

At minimum: `DATABASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`, `QDRANT_URL`,
`QDRANT_API_KEY`, `QDRANT_COLLECTION`, and `MCP_API_KEYS`.

## Point the domain at the server

In your DNS provider, add an **A record**: `podsearch.space -> YOUR_SERVER_IP`.
Caddy fetches the HTTPS certificate automatically once DNS has propagated. Set the
matching domain in the [`Caddyfile`](Caddyfile).

## Start everything

```bash
docker compose up -d --build
```

Check it's healthy:

```bash
docker compose ps         # web, pipeline, caddy should all be "running"
docker compose logs -f    # follow logs (Ctrl-C to stop)
```

Visit **https://podsearch.space** — the site should load. Confirm the API is live
from outside via `https://podsearch.space/api/health`.

---

## Deploying updates later

Push your changes to `main`, then on the server run:

```bash
cd ~/podsearch.space
./deploy.sh        # git reset --hard origin/main + rebuild + restart + prune
```

`.github/workflows/deploy.yml` can run this for you over SSH on every push to `main` —
set the repo secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH`.

## Handy commands

```bash
docker compose ps                  # what's running
docker compose logs -f web         # API logs
docker compose logs -f pipeline    # indexer logs
docker compose restart web         # restart just the API
docker compose down                # stop everything
docker compose up -d               # start everything (no rebuild)
```

## Notes

- **Oversize audio:** episodes over 50 MB are compressed by the `pipeline` container
  into the shared `audio` volume. To let the gateway fetch them over HTTPS, set
  `LIVE_AUDIO_BASE_URL` and expose that path (e.g. an extra `handle_path` in the
  Caddyfile). Most feeds never hit this path.
- **Running Qdrant on the droplet (optional):** if you'd rather self-host Qdrant
  alongside the app, add a `qdrant/qdrant` service to `docker-compose.yml` and point
  `QDRANT_URL` at `http://qdrant:6333`.
