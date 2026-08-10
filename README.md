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

v1's executor is a **lightweight real fetch**: it GETs the task's URL and extracts the page title +
meta tags — enough to prove the whole loop end-to-end before wiring up the full browser crawl. See
`crawlfast_external_worker/executor.py` for the extension point to reach parity with the internal
Playwright worker later.

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
