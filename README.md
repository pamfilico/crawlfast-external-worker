# crawlfast external worker

A tiny, self-contained **pull-based** crawl node. Drop it on any spare machine (an old laptop on
the LAN), give it a URL and an API key, and it will poll the crawlfast API for work, do it, and
report back. It knows **nothing** about the backend's database or secrets — its whole world is a
`config.yaml` with an `api_base_url` and an `api_key`.

> **Pull-only by design.** The node dials the server; the server never dials the node. So there's
> nothing to expose, no inbound ports, no tunnel required. Health = "did the node heartbeat
> recently". (Server→node health/SSH orchestration comes later, once a tunnel/ngrok exists.)

## How it works

Each cycle the worker:
1. **heartbeats** — `POST /api/v1/external-worker/heartbeat` (so the server knows it's alive),
2. **claims** one task — `POST /api/v1/external-worker/tasks/claim` (atomic; nothing if idle),
3. **executes** it and **reports** — `POST /api/v1/external-worker/tasks/{id}/result`.

Auth is the `X-Worker-Api-Key` header on every call.

The executor (no browser, pure `requests`):
- **`crawl_single` / `extract_meta`** — fetch one page, extract title + meta.
- **`crawl_all` / `crawl` / `crawl_sitemap`** — **BFS the whole site**: follow same-host links and
  crawl every page up to `payload.max_pages` (default 50), streaming incremental progress and
  returning `pages_crawled` + `elapsed_ms` (the total time to clone the site).

See `crawlfast_external_worker/executor.py` for the extension point to reach parity with the
internal Playwright worker later — same handler signature.

## Scaling — run 2+ workers

```bash
docker compose up --build --scale worker=4      # 4 concurrent pullers, one node/key
```
No `container_name`, so `--scale` just works. All replicas share this node's key and poll the same
queue; claims are atomic (`FOR UPDATE SKIP LOCKED`) so no task is ever processed twice — you get N×
throughput. For **distinct nodes** (separate identities, e.g. one per laptop) each with its own key:

```bash
export CRAWLFAST_WORKER_API_BASE_URL=http://192.168.1.10:5053
export WORKER1_KEY=cfw_...  WORKER2_KEY=cfw_...
docker compose -f docker-compose.multi.yml up --build   # node-1 + node-2
```

## Provision a node from scratch (old or new machine)

One repeatable command turns a bare Linux/macOS box into a running node — installs prereqs, writes
`config.yaml`, and (with `--service`) installs a systemd service that runs on boot and auto-restarts:

```bash
# fresh machine (no git yet) — Debian/Ubuntu:
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/pamfilico/crawlfast-external-worker.git
cd crawlfast-external-worker
./setup-node.sh --api-url http://192.168.1.47:5099 --api-key cfw_XXX --name node-1 --service
```
`setup-node.sh` is idempotent (re-run to update/reconfigure). Drop `--service` to run in the
foreground, or use `--once` for a single cron-style cycle. Deps are reused if already present, else
a venv (or `pip --user`) is created — no manual Python setup.

Manage the service: `journalctl -u crawlfast-worker -f` · `systemctl restart crawlfast-worker`.

### mDNS / `.local` — beat DHCP IP drift

On Linux the provisioner installs **avahi** and aligns the hostname to `--name`, so the node is
reachable as **`<name>.local`** regardless of its DHCP IP (`ssh crawlfast-node1@crawlfast-node1.local`).
It also lets the node **resolve other `.local` names** — so point `--api-url` at the API box's mDNS
name instead of its IP and the node survives the *server's* IP changing too:

```bash
./setup-node.sh --api-url http://my-macbook.local:5099 --api-key cfw_XXX --name crawlfast-node1 --service
```
(macOS advertises `.local` natively via Bonjour — `scutil --get LocalHostName` shows its name.)

## Configure

```bash
cp config.example.yaml config.yaml
# edit:
#   api_base_url: http://<backend-LAN-ip>:5053   (cloud later: https://api.crawlfa.st)
#   api_key:      cfw_...                          (provisioned server-side; worker A → key A)
```

Everything can also come from env vars (they win over the file), handy for Docker/cron:
`CRAWLFAST_WORKER_API_BASE_URL`, `CRAWLFAST_WORKER_API_KEY`, `CRAWLFAST_WORKER_NAME`,
`CRAWLFAST_WORKER_POLL_INTERVAL`, `CRAWLFAST_WORKER_TIMEOUT`, `CRAWLFAST_WORKER_CONFIG`.

## Run

### Docker (the laptop path)
```bash
cp config.example.yaml config.yaml && $EDITOR config.yaml
docker compose up --build            # long-running poll loop
```

### Plain Python
```bash
pip install -r requirements.txt
python -m crawlfast_external_worker.worker                 # poll loop
python -m crawlfast_external_worker.worker --once          # single cycle (cron)
python -m crawlfast_external_worker.worker --health         # just ping the API
```

### Cron (every minute, single-shot)
```cron
* * * * * cd /opt/crawlfast-external-worker && CRAWLFAST_WORKER_CONFIG=/opt/crawlfast-external-worker/config.yaml /usr/bin/python3 -m crawlfast_external_worker.worker --once >> /var/log/crawlfast-worker.log 2>&1
```

## Provisioning a worker (server side, ops)

Ask the crawlfast API (needs the admin secret `EXTERNAL_WORKER_ADMIN_SECRET`):

```bash
# create a worker + get its key (shown once)
curl -sX POST http://<backend>/api/v1/internal/external-workers \
  -H "X-Crawlfast-Internal-Secret: $EXTERNAL_WORKER_ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"name":"crawlfast-node1"}'

# enqueue a task for workers to pull
curl -sX POST http://<backend>/api/v1/internal/external-worker-tasks \
  -H "X-Crawlfast-Internal-Secret: $EXTERNAL_WORKER_ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"task_type":"crawl_single","payload":{"url":"https://example.com"}}'
```

## Layout
```
crawlfast_external_worker/
  config.py     load YAML + env → WorkerConfig
  client.py     HTTP client for the worker API (X-Worker-Api-Key)
  executor.py   task_type → handler registry (v1 lite fetch; extend for full crawl)
  worker.py     main loop (--once cron mode, --health)
config.example.yaml  Dockerfile  docker-compose.yml  requirements.txt
```
