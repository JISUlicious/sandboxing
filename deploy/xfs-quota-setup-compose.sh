#!/usr/bin/env bash
# Compose-path entry for SPEC-302 quota setup.
# The control-plane container runs userns_mode=host with CAP_SYS_ADMIN,
# so /usr/local/bin/sandbox-quota-helper can be invoked directly — no
# sudo needed (and no sudoers config exists inside the image).
set -euo pipefail
exec /usr/local/bin/sandbox-quota-helper setup
