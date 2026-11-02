FROM python:3.12.13-slim-bookworm AS wheel
WORKDIR /src
COPY . .
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12.13-slim-bookworm
ARG SCOPE_RECALL_VERSION
LABEL org.opencontainers.image.version=$SCOPE_RECALL_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends supervisor tini \
    && groupadd --gid 10001 scope && useradd --uid 10001 --gid scope --create-home scope \
    && mkdir -p /data/store /data/homes /etc/scope-recall && chown -R scope:scope /data
COPY --from=wheel /wheels /wheels
RUN for wheel in /wheels/*.whl; do python -m pip install --no-cache-dir "$wheel[codex,lancedb]"; done \
    && python -c 'import importlib.metadata, os; assert importlib.metadata.version("hermes-scope-recall") == os.environ["SCOPE_RECALL_VERSION"]'
COPY distribution/docker/supervisord.conf /etc/scope-recall/supervisord.conf
COPY distribution/docker/wake-worker.sh /usr/local/bin/scope-recall-wake-worker
USER 10001:10001
ENV HOME=/data
WORKDIR /data
ENTRYPOINT ["tini", "--", "python", "-m", "scope_recall.distribution.docker.entry"]
