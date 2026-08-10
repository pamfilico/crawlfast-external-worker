# crawlfast external worker — runs on a remote node (old laptop on the LAN).
# Tiny image: just Python + requests + pyyaml. No browser, no backend code.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY crawlfast_external_worker /app/crawlfast_external_worker

# Config is provided at runtime via a mounted config.yaml or CRAWLFAST_WORKER_* env vars.
ENTRYPOINT ["python", "-m", "crawlfast_external_worker.worker"]
CMD []
