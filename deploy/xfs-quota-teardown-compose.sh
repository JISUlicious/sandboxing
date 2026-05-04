#!/usr/bin/env bash
# Compose-path entry for SPEC-302 quota teardown. See the setup
# wrapper for why no sudo.
set -euo pipefail
exec /usr/local/bin/sandbox-quota-helper teardown
