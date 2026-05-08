#!/usr/bin/env bash
# System-tools smoke test for sandbox-runtime-ds.
# Confirms LibreOffice + pandoc are installed and on PATH — the two
# system tools Anthropic's xlsx/docx skills shell out to.
# Exits 0 on success, 1 if either tool is missing or fails.

set -euo pipefail

fail=0

if soffice --version >/dev/null 2>&1; then
    printf '  soffice: %s\n' "$(soffice --version | head -n1)"
else
    echo "  soffice: MISSING" >&2
    fail=1
fi

if pandoc --version >/dev/null 2>&1; then
    printf '  pandoc: %s\n' "$(pandoc --version | head -n1)"
else
    echo "  pandoc: MISSING" >&2
    fail=1
fi

if (( fail )); then
    echo "system-tools: FAIL" >&2
    exit 1
fi
echo "system-tools: OK"
