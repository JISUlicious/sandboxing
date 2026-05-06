# Sandbox runtime image — see ARCH-030..033, ARCH-031.
# Built once, pre-pulled at host boot, never modified at runtime.
FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        nodejs \
        npm \
        git \
        ripgrep \
        build-essential \
        ca-certificates \
        curl \
        procps \
        less \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /workspace \
       --shell /bin/bash agent \
    && chmod 0750 /workspace

USER 10001:10001
WORKDIR /workspace
ENV HOME=/workspace USER=agent

# Container is kept alive by the control plane; exec calls run via
# `docker exec`. The entrypoint must not exit on its own.
#
# tini as PID 1 reaps zombie children so the slice-11 background-
# process feature doesn't leak defunct entries when supervised
# subprocesses die. tini forwards signals so `docker stop` still
# tears the container down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/sleep", "infinity"]
