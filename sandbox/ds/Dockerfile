# Sandbox runtime image — data-science variant.
#
# Same security posture as sandbox/Dockerfile (agent UID 10001,
# /workspace home, tini PID 1) plus a preloaded Python data-science /
# ML stack and the Node deps for Anthropic's pptx/docx skills.
#
# Operators select this image at deploy time via SANDBOX_SANDBOX_IMAGE
# in /etc/sandbox/env; no API code change. See docs/DEPLOY.md
# "Choosing the runtime image" for the variant table + bundled-package
# list.
#
# Image size ~5–7 GiB compressed (heavyweight optionals included).

FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

# ----- system packages -----
# Read the apt list from system-packages.txt so this Dockerfile and
# the install set stay in sync without duplication. `xargs -a` skips
# blank lines + comments via `grep -v` first.
COPY system-packages.txt /tmp/system-packages.txt
RUN grep -v '^\s*#' /tmp/system-packages.txt | grep -v '^\s*$' \
    | xargs apt-get update \
    && grep -v '^\s*#' /tmp/system-packages.txt | grep -v '^\s*$' \
    | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /tmp/system-packages.txt

# ----- Python venv at /opt/ds-venv -----
# A system-wide venv keeps Debian's PEP 668 EXTERNALLY-MANAGED marker
# intact while letting `python3` find the data-science libs. Install
# uv inside the venv to speed up the heavyweight pip install
# (catboost / xgboost / shap / dask / dowhy / econml together would
# take ~10 min with stock pip; uv brings it to ~3 min).
RUN python3 -m venv /opt/ds-venv \
    && /opt/ds-venv/bin/pip install --no-cache-dir --upgrade pip uv

COPY requirements.txt /opt/ds/requirements.txt
RUN /opt/ds-venv/bin/uv pip install --no-cache-dir \
        --python /opt/ds-venv/bin/python3 \
        -r /opt/ds/requirements.txt

# ----- Node deps for Anthropic pptx/docx skills -----
# Globally requireable via NODE_PATH so per-session npm install isn't
# needed. `npm ci` would need a package-lock.json; npm install on a
# pinned package.json is acceptable for a build-time, single-shot use.
COPY package.json /opt/ds-node/package.json
RUN cd /opt/ds-node \
    && npm install --omit=dev --no-audit --no-fund --loglevel=warn \
    && npm cache clean --force

# ----- smoke tests (run by GHA after build, ARCH-033) -----
COPY smoke.py /opt/ds/smoke.py
COPY smoke.js /opt/ds/smoke.js
COPY smoke.sh /opt/ds/smoke.sh
RUN chmod +x /opt/ds/smoke.sh

# ----- agent user (matches sandbox/Dockerfile exactly) -----
RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /workspace \
       --shell /bin/bash agent \
    && chmod 0750 /workspace

# Make the venv + node modules available to every shell + every exec.
# /etc/profile.d covers login shells; ENV exports cover docker exec
# (which doesn't go through login).
RUN printf '%s\n' \
        'export PATH=/opt/ds-venv/bin:$PATH' \
        'export NODE_PATH=/opt/ds-node/node_modules' \
        > /etc/profile.d/ds-runtime.sh \
    && chmod 0644 /etc/profile.d/ds-runtime.sh
ENV PATH="/opt/ds-venv/bin:${PATH}"
ENV NODE_PATH="/opt/ds-node/node_modules"

USER 10001:10001
WORKDIR /workspace
ENV HOME=/workspace USER=agent

# Container is kept alive by the control plane; exec calls run via
# `docker exec`. Same entrypoint as the minimal image — tini reaps
# zombies for slice-11 background processes and forwards signals.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/sleep", "infinity"]
