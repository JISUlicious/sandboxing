# sandbox-runtime-ds

Data-science variant of the sandbox runtime image. Bundles a
preloaded Python / Node / system-tools stack so per-session
sandboxes can do data analysis, ML, and pptx/xlsx/docx
generation without per-session `pip install` or `npm install`
overhead.

Operators select this image at deploy time via
`SANDBOX_SANDBOX_IMAGE` in `/etc/sandbox/env`. The control plane
and `compose.yml` need no changes — the existing knob already
flows through.

See [docs/DEPLOY.md "Choosing the runtime image"](../../docs/DEPLOY.md)
for the variant-vs-default trade-offs and switch instructions.

## What's inside

### Python (`/opt/ds-venv`)

System-wide venv. Pinned in [`requirements.txt`](./requirements.txt):

- **Core**: pandas≥2.3, numpy, scipy, pyarrow.
- **Plotting**: plotly + kaleido (static export), matplotlib, seaborn.
- **ML**: scikit-learn, xgboost, catboost, shap, umap-learn.
- **Stats / RCA**: statsmodels, ruptures, mlxtend, numba.
- **Heavyweight optionals**: dowhy, econml, dask[complete].
- **IO**: openpyxl, sqlalchemy, psycopg2-binary,
  adbc-driver-postgresql, lxml.
- **Anthropic skills**: markitdown, python-pptx, python-docx,
  pillow.

`PATH` is set so `python3` and `pip` resolve to the venv. The
agent user picks this up automatically — no `source activate`
needed.

### Node (`/opt/ds-node`)

Pinned in [`package.json`](./package.json):

- `pptxgenjs` — Anthropic pptx skill, create-from-scratch.
- `docx` — Anthropic docx skill, create-from-scratch (the
  SKILL.md sometimes refers to this as "docx-js"; the npm
  package is `docx`).

`NODE_PATH=/opt/ds-node/node_modules` so `require('pptxgenjs')`
and `require('docx')` work without per-session npm install.

**No `exceljs`.** Verified by reading
`anthropics/skills/skills/xlsx/scripts/`: the xlsx skill is pure
Python (`openpyxl` for create / edit + LibreOffice's `soffice`
for formula recalc).

### System tools

apt-installed via [`system-packages.txt`](./system-packages.txt):

- `libreoffice-core / writer / calc / impress` + `soffice` — used
  by Anthropic's xlsx skill (formula recalc) and docx skill
  (legacy `.doc` → `.docx`).
- `pandoc` — Anthropic docx skill content reading.
- `libomp1` — xgboost runtime dependency.
- `ghostscript` — PDF / image fallback used by some skill scripts.
- `fonts-dejavu-core` — consistent rendering for pptx/docx.

## Smoke tests

Three scripts at `/opt/ds/`:

- `smoke.py` — imports every Python lib in `requirements.txt`.
- `smoke.js` — requires every Node lib in `package.json`.
- `smoke.sh` — checks `soffice` and `pandoc` are on PATH.

GHA runs all three after `docker build` and before pushing to
GHCR. Local dev:

```bash
docker build -t sandbox-runtime-ds:dev .
docker run --rm sandbox-runtime-ds:dev python3 /opt/ds/smoke.py
docker run --rm sandbox-runtime-ds:dev node /opt/ds/smoke.js
docker run --rm sandbox-runtime-ds:dev /opt/ds/smoke.sh
```

## Adding a package

1. Pin the new version in `requirements.txt` (Python) or
   `package.json` (Node) or `system-packages.txt` (apt).
2. Add the import / require to the matching smoke script.
3. Rebuild + run smoke tests locally.
4. Commit + cut a release tag — the GHA matrix builds + pushes
   the new image automatically.

Image size grows ~linearly with package count. Today's image is
~5–7 GiB compressed; a single new package usually adds <50 MiB,
but heavyweights (CV / DL / GIS) can each add hundreds of MiB —
weigh the cost.

## What's NOT inside

- **GPU / CUDA** — explicitly out of scope; production posture
  doesn't need it and gVisor's GPU support is experimental.
- **TensorFlow / PyTorch / JAX** — would add ~3 GiB more. Add as
  a third variant later if there's demand.
- **Jupyter / Jupyterlab** — the HTTP `/v1/sessions/{sid}/exec`
  endpoint is the interaction layer; in-container Jupyter would
  conflict.
- **pd-skills' `.md` files or Anthropic skills' `.md` files** —
  skills are prompt material loaded by Claude Code from the
  operator's filesystem or a marketplace. The runtime image
  ships only the libs the skills reference.
