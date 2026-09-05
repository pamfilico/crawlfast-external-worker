# The worker's own tests

`make test-worker` (from the repo root), or `.venv/bin/python -m pytest` here.

No docker, no database, no network — the suite serves its own website and its own crawlfast API on
127.0.0.1 and runs in about eight seconds. Run it on every change to this package.

## Why this package is tested separately, and hard

This code runs on machines nobody can reach: laptops and Raspberry Pis on other people's networks,
behind their routers. The server **pulls nothing and pushes nothing** — the node claims work. Three
consequences shape every test here:

1. **A protocol break does not raise anywhere.** The node keeps heartbeating and simply claims
   nothing. From the dashboard that is indistinguishable from an idle queue, which is the
   "online but running 0" state in `NODE-OPS.md`.
2. **A node cannot be redeployed from CI.** Shipping a break means visiting each machine.
3. **A crawl can fail while reporting success.** Pages fetched but refused, or posted but never
   stored, still produce `status: succeeded`. `CRAWL_HEALTH.md` is the writeup; the counts in
   `test_worker_loop.py` are the guard.

## The files

| file | what it holds the line on |
|---|---|
| `test_client_protocol.py` | **The wire contract.** Every envelope field the client reads, by name, with the reader cited. Mirrored by `backends/crawlfast-backend/tests/contract/test_external_worker_protocol.py`, which asserts the *server* still sends them. Two suites, one contract. |
| `test_executor_crawl.py` | Link discovery and the traps it was written against: `&amp;` in hrefs, `/en/en/` duplicate segments, assets behind cache-busting queries, non-HTML bodies, 429 + `Retry-After`. |
| `test_encoding.py` | Character encoding. `requests` decodes a `text/html` response with no `charset` parameter as **ISO-8859-1**; most sites declare UTF-8 in a `<meta>` tag instead. Every such page was stored mojibake and nothing ever failed. |
| `test_page_spool.py` | saved / rejected / spooled — the server's verdict, never the HTTP status. And that a page whose task was reassigned is *dropped*, not re-flushed forever. |
| `test_worker_loop.py` | Claim → run → report, and that the reported counts are the server's answers. |
| `test_watchdog.py` | The self-heal: no forward progress for `WORKER_WATCHDOG_IDLE_SECONDS` exits non-zero so `restart: unless-stopped` respawns a clean worker. |
| `test_config.py` | Every transport opt-in defaults **off**. A flag that defaults on changes every node in the field the day it merges. |

## Fixtures

`conftest.py` starts two in-process HTTP servers:

- **`site`** — serves `tests/fixtures/site`, a small website built to contain the exact link shapes
  that have cost pages in production. A test can rewrite any path's status, content type, body or
  headers (`site.set(...)`) and read back what was requested (`site.hits`).
- **`api`** — the crawlfast external-worker API, answering the real `standard_response` envelope.
  It can be told to fail a route N times (`api.fail_next`), answer 404 like an older backend
  (`api.missing`), or refuse pages (`api.reject_pages`).

The envelope in `conftest.envelope()` is **spelled out, not imported**. Importing the server's own
helper would make a change to it invisible here, which is exactly the change these tests exist to
catch.

A session fixture replaces `socket.create_connection` and `socket.socket.connect` with versions
that refuse anything but loopback. A crawler's test suite must never be one typo away from crawling
the internet.

## Where the other halves live

- `backends/crawlfast-backend/tests/contract/` — the server side of the same contract.
- `tests/integration/test_external_worker_live.py` — **this package**, mounted read-only into the
  integration runner and driven against the real crawlfast container crawling real nginx-served
  pages. Both unit suites can pass while the two sides disagree; that file is what closes the gap.
