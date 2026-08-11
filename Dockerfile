# crawlfast external worker — runs on a remote node (laptop / Raspberry Pi on the LAN).
# Tiny, multi-arch (amd64/arm64/armhf) image: Python + requests + pyyaml. No browser, no backend code.
#
# mDNS: libnss-mdns + an nsswitch tweak so the container can resolve `.local` names (e.g. the API
# box) itself — needs `network_mode: host` at runtime so multicast reaches the LAN. This is what
# makes the node survive the server's DHCP IP drift (point --api-url at <host>.local).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libnss-mdns \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^hosts:.*/hosts: files mdns4_minimal [NOTFOUND=return] dns/' /etc/nsswitch.conf

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY crawlfast_external_worker /app/crawlfast_external_worker

# Config is provided at runtime via a mounted config.yaml or CRAWLFAST_WORKER_* env vars.
ENTRYPOINT ["python", "-m", "crawlfast_external_worker.worker"]
CMD []
