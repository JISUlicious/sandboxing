from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_", extra="ignore")

    dev_mode: bool = False
    api_token: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # Slice 7 — multi-tenant identity (SPEC-405).
    # token_pepper is HMAC'd with the bearer token to produce the hash
    # stored in the tokens table; rotating it invalidates ALL tokens
    # so set it once and never change. Generate via:
    #     openssl rand -hex 32
    # Required when multi-tenant is in play; left empty in unit-test
    # configs so the default ("") is hashed deterministically.
    token_pepper: str = ""
    # Grace period after rotation during which the old token still
    # authenticates. SPEC-405 has no specific number; 5 minutes is
    # a reasonable client refresh window.
    token_grace_seconds: int = 300

    # TLS-readiness (slice 8e). The control plane stays plain HTTP on
    # 127.0.0.1; an upstream reverse proxy (Caddy / nginx) terminates
    # TLS and forwards. Set this True only when running BEHIND such a
    # proxy — otherwise spoofed X-Forwarded-* headers would let any
    # caller fake their source IP / scheme.
    trust_proxy_headers: bool = False

    db_path: Path = Path("./var/sandbox.db")
    audit_log_path: Path = Path("./var/audit.log")
    audit_fallback_log_path: Path | None = None  # default: <audit_log>.fallback.jsonl
    audit_buffer_timeout_s: float = 5.0  # ARCH §7

    sandbox_image: str = "sandbox-runtime:latest"
    network_name: str = "sandbox_egress"
    # Proxy URL injected as HTTP(S)_PROXY into every sandbox. Use an IP
    # if you're on a runtime where Docker's embedded DNS can't be
    # reached from inside the sandbox (e.g., some gVisor versions).
    egress_proxy_url: str = "http://proxy:3128"

    default_vcpu: int = 2
    default_memory_mib: int = 2048
    default_workspace_mib: int = 1024
    default_pids: int = 256
    default_nofile: int = 1024
    default_exec_timeout_s: int = 60

    idle_stop_minutes: int = 15
    hard_destroy_hours: int = 24
    reaper_interval_s: int = 60
    # Slice 13c — activity pinning. When True (default), mutating
    # ops (exec, file write/read/delete, process start/delete,
    # log-stream open) bump `last_activity_at` so a busy RUNNING
    # session stays alive across the idle_stop_minutes window.
    # When False, only state transitions bump activity (the
    # pre-13c semantic, where active sessions get implicitly
    # idle-stopped after N minutes from create and auto-resumed
    # on the next op). Hard-destroy TTL applies regardless.
    pin_on_activity: bool = True
    # SPEC-501 — per-session cpu/mem/blkio samples. Set to 0 to disable.
    resource_sample_interval_s: int = 10

    # Slice 13a — orphan-resource sweeper. Detects Docker volumes and
    # containers labelled with `sandbox.session_id` that the registry
    # doesn't know about (mid-create crash, registry restored from old
    # backup, operator DB edit). Complementary to the main reaper,
    # which handles the inverse direction.
    orphan_reap_interval_s: int = 3600  # slow tick; not latency-sensitive
    # Only reap resources whose Docker-side `Created` timestamp is older
    # than this. Absorbs the sub-second create-path race (volume →
    # container → registry insert) and post-crash startup
    # inconsistency before reconciliation completes. Lower for testing.
    orphan_reap_grace_s: int = 3600
    # Hard cap per tick so a registry wipe or restore-from-old-backup
    # can't shred everything in one sweep. Operators can raise this
    # once they've confirmed the sweeper is well-behaved.
    orphan_reap_max_per_tick: int = 10

    tenant_max_concurrent: int = 50

    # SPEC-302 quota hooks. Both empty by default — no-op in dev mode.
    # In production point these at the deploy/xfs-quota-{setup,teardown}.sh
    # examples (or any script that takes SESSION_ID, VOLUME_NAME,
    # VOLUME_PATH, WORKSPACE_MIB env vars and returns 0 on success).
    quota_setup_cmd: str = ""
    quota_teardown_cmd: str = ""
    # Mountpoint where per-session volume directories live (passed to
    # the quota scripts as VOLUME_BASE).
    quota_volume_base: Path = Path("/var/lib/sandbox-volumes")

    # SPEC-401 — host UID that container UID 10001 maps to under
    # `userns-remap=default`. With it set, per-session bind directories
    # are chown'd to this UID and chmod'd 0700 (no more world-writable
    # 0777 stopgap). Compute via (note +10001 — agent UID is 10001
    # inside the container, NOT 10000):
    #     awk -F: '$1=="dockremap"{print $2 + 10001}' /etc/subuid
    # Leave None on dev / non-userns-remap hosts; create_volume falls
    # back to the 0777 mode and warns at startup.
    bind_volume_uid: int | None = None

    # Slice 11a — Idempotency-Key cache TTL. Stripe's reference value
    # is 24h; tune lower if storage pressure is a concern.
    idempotency_ttl_s: int = 86_400

    # Slice 11b — background-process tunables.
    # `process_watcher_interval_s` paces the lazy state refresh on
    # `GET /processes`/`/processes/{pid}` calls — we don't poll
    # liveness more often than this for a given row to avoid
    # hammering the docker daemon.
    process_watcher_interval_s: float = 2.0
    # SIGTERM → wait → SIGKILL grace window on `DELETE /processes/{pid}`.
    process_stop_grace_s: int = 10
    # Tenant-wide ceiling on `Limits.max_processes`. The Limits default
    # is 8; operators raise the per-session field with the clamp here.
    tenant_max_processes: int = 32

    # Slice 12 — admin token for the tenant-management API. Optional;
    # if unset, /v1/tenants admin endpoints return 503 admin_disabled.
    # Single-tenant deployments don't need to set this. Generate via:
    #     openssl rand -hex 32
    admin_token: str = ""


settings = Settings()
