"""Prometheus instrumentation. SPEC-503, SPEC-504.

Metrics are defined as module-level singletons so the registry sees one
copy per process. Tests pass because each test gets a fresh process? No —
pytest reuses the process; we explicitly clear histograms by recreating
them in a custom registry per app, but for slice 4 we share the default.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ----- API surface metrics -----

api_requests_total = Counter(
    "sandbox_api_requests_total",
    "API requests served, labelled by method, templated path, and status code.",
    labelnames=("method", "path", "status"),
)

api_request_duration_seconds = Histogram(
    "sandbox_api_request_duration_seconds",
    "End-to-end API handler latency.",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ----- session lifecycle -----

sessions_lifecycle_total = Counter(
    "sandbox_sessions_lifecycle_total",
    "Counts of lifecycle transitions; reason captures idle/ttl/api/error.",
    labelnames=("transition", "reason"),
)

sessions_by_status = Gauge(
    "sandbox_sessions_by_status",
    "Number of sessions in each status (sampled by the reaper).",
    labelnames=("status",),
)

session_create_seconds = Histogram(
    "sandbox_session_create_seconds",
    "Time spent in CreateSession from request to RUNNING.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

# ----- exec + resume (SPEC-502, SPEC-504) -----

exec_duration_seconds = Histogram(
    "sandbox_exec_duration_seconds",
    "docker-exec wall-clock duration as reported by the runtime.",
    labelnames=("result",),  # ok | timeout
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 30, 60, 600),
)

resume_seconds = Histogram(
    "sandbox_resume_seconds",
    "Time to resume a STOPPED/IDLE session, separate from exec overhead (SPEC-504).",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# ----- audit -----

audit_emit_total = Counter(
    "sandbox_audit_emit_total",
    "Audit log records written, labelled by record kind.",
    labelnames=("kind",),
)

# ----- resource sampler (slice 6b) -----

# Counter, not per-session gauge — per-session labels would explode
# cardinality on a busy host. Per-session details land in the audit
# log (`kind="session.sample"`). The aggregate signal here is just
# "is the sampler healthy".
resource_samples_total = Counter(
    "sandbox_resource_samples_total",
    "Resource samples taken by the per-session sampler.",
    labelnames=("result",),  # ok | error
)

resource_sample_duration_seconds = Histogram(
    "sandbox_resource_sample_duration_seconds",
    "Wall-clock duration of a single sampler sweep.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
