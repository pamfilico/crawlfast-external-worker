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

## Self-healing (worker.py)
The poll loop swallows transient errors (never die on a blip) but a **watchdog** exits the process
after `WORKER_WATCHDOG_IDLE_SECONDS` (default 600) of no task processed → `restart: unless-stopped`
respawns a fresh worker. So a wedged claim loop now heals itself with no SSH. To activate a code
change on nodes: node self-update (git pull + rebuild) or `docker compose up -d --build worker`.
