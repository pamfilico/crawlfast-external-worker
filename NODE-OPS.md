# Crawlfast crawl-node fleet — ops runbook

External worker nodes (laptops/Pis) PULL crawl tasks from crawlfast-backend. The server never
reaches into a node; nodes call the API with their own key.

## Reach a node (SSH)
The SSH user == the node name (set at onboard):
```
ssh crawlfast-node1@crawlfast-node1.local     # 192.168.1.201
ssh crawlfast-node2@crawlfast-node2.local     # 192.168.1.202
```

## Worker runtime
Runs as **docker compose** in the node's crawlfast-external-worker checkout (services
`crawlfast-external-worker-worker-1/-2`, `log-pusher`), `restart: unless-stopped`.
(Some nodes use the systemd unit `crawlfast-worker.service` instead — `systemctl restart` it.)
```
ssh crawlfast-nodeN@crawlfast-nodeN.local 'cd crawlfast-external-worker && docker compose restart worker'
# logs:  docker compose logs -f worker      # or: journalctl -u crawlfast-worker -f
```

## Check the fleet (the ONLY correct status source)
```
docker exec suite_local_crawlfast_backend python -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://localhost:5000/api/v1/internal/external-workers',timeout=15))['data'];print('live',d['live_count'],'queue',d['queue']);[print(w['name'],w['status'],'seen',w['seconds_since_seen'],'s running',w['running']) for w in d['workers'] if w['seconds_since_seen']<1800]"
```
- Tasks live in the DB table `external_worker_task` (status pending/claimed/running/succeeded/failed),
  NOT the legacy redis `crawfast:queue` (unused — ignore it).
- Nodes PULL via `POST /api/v1/external-worker/tasks/claim`; **heartbeat is a SEPARATE call**
  (`/heartbeat`). So a node can be `online` (heartbeating) yet `running 0` with every pending task
  `attempts=0` = its **claim loop is wedged**. That's node-side; restart the worker.

## Is the fleet fast enough? (pacing)
`fleet/health` says the fleet is ALIVE. It cannot say whether the queue or the fleet is the limit —
a depth reads the same for a healthy buffer and a stalled backlog. That is the pacing read:
```
docker exec suite_local_crawlfast_backend python -c "
import urllib.request,json
d=json.load(urllib.request.urlopen('http://localhost:5000/api/v1/internal/crawl-metrics/pacing?window_minutes=60&interval_seconds=300',timeout=60))['data']
print(d['constraint']['limited_by'],'|',d['constraint']['verdict'])
for n in d['nodes']:
    if n['live']: print(' ',n['name'],n['tasks_per_hour'],'sites/hr  running',n['running'],' p50',n['p50_task_seconds'],'s')"
```
- `limited_by=fleet` -> work is queueing behind busy slots; **more workers = more throughput**.
- `limited_by=queue` -> nodes claim instantly and idle; adding nodes buys nothing.
- `running` is the node's live concurrency — every replica shares one key, so this is the ONLY
  place a node's worker count is observable.

Full reference: **`CRAWL_PACING.md`** at the repo root. UI: `/queue/queues?tab=crawlfast` -> Speed.

## Spool health (the 16-day silent failure)
Pages that fail to POST are spooled to `.page-spool/` and retried on every heartbeat, **inline in
the worker loop**. If they can never land, the node burns its uplink and roughly half its wall time
re-uploading them forever.
```
find crawlfast-external-worker/.page-spool -name "*.json" | wc -l      # want 0
docker compose logs --since 1h worker | grep "spool flush"
```
- `recovered=N dropped=N remaining=falling` -> healthy, draining.
- **`recovered=0 dropped=0 remaining=<not falling>` -> STUCK.** This is the tripwire. It ran unseen
  for 16 days (4,459 files / 1.27 GB on node2) because the server answered 500 for a task the
  worker no longer owned, and the spool only drops on "not assigned"/"not found"/"404".

Root cause + fixes: **`CRAWL_TASK_LEASE.md`** at the repo root.

## Self-healing (worker.py)
The poll loop swallows transient errors (never die on a blip) but a **watchdog** exits the process
after `WORKER_WATCHDOG_IDLE_SECONDS` (default 600) of no task processed → `restart: unless-stopped`
respawns a fresh worker. So a wedged claim loop now heals itself with no SSH. To activate a code
change on nodes: node self-update (git pull + rebuild) or `docker compose up -d --build worker`.

## The browser fingerprint (a constant that expires)

The node fetches pages with a **pinned Chrome version**. That pin is not a style choice — it is the
difference between a crawl and a wall of `403`s, and it goes stale on its own.

`Chrome/124` was current when it was pinned and was answered `200`. It aged into a total block from
Akamai-fronted origins while the file sat untouched: no commit, no deploy, nothing changed on our
side. It cost **2,947 pages across 100+ hosts** before anyone looked.

```python
# crawlfast_external_worker/executor.py
_CHROME_MAJOR = 152          # what a real desktop Chrome reports
_UA_PINNED_ON = date(2026, 9, 8)   # when that was last checked
_UA_MAX_AGE_DAYS = 180
```

**If CI fails `test_the_pinned_chrome_version_has_not_gone_stale`:** open a real Chrome, read
`chrome://version`, set `_CHROME_MAJOR` to that major and `_UA_PINNED_ON` to today. That is the
whole fix. Do not silence the test — its only job is to fire before the cliff.

**Symptom on the fleet:** pages failing with `http 403` in clusters, several hosts at once, while
`errors == 0` on every task (a 403 is a successful HTTP exchange, so nothing raises). Confirm with:

```
GET /api/v1/internal/crawl-failure-reasons?hours=720&status=403     # on crawlfast-backend
```

If one host shows dozens of blocked pages, that is one problem with one fix, not dozens of dead
sites. Note that pacing is the intuitive cause and usually the wrong one — `429`, not `403`, is the
status that means slow down.

Full writeup: **`CRAWL_BLOCKING.md`** at the monorepo root.
