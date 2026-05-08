"""Import-only smoke test for sandbox-runtime-ds.

Run inside the image (root or agent user) to confirm every bundled
Python library imports cleanly. Exits 0 on success, 1 on the first
failed import.

Used by GHA after `docker build` — failure here blocks the release.
"""

import importlib
import sys

# Mirror sandbox/ds/requirements.txt; one entry per top-level module
# the operator should be able to `import` without per-session pip work.
MODULES = [
    # pd-skills + general DS core
    "pandas",
    "numpy",
    "scipy",
    "pyarrow",
    "plotly",
    "matplotlib",
    "seaborn",
    "sklearn",
    "xgboost",
    "catboost",
    "shap",
    "umap",
    "statsmodels",
    "ruptures",
    "mlxtend",
    "numba",
    # Heavyweight optionals
    "dowhy",
    "econml",
    "dask",
    # Data IO
    "openpyxl",
    "sqlalchemy",
    "adbc_driver_postgresql",
    "lxml",
    # Anthropic skills (pptx/xlsx/docx — Python side)
    "markitdown",
    "pptx",
    "docx",
    "PIL",
]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for mod in MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            failures.append((mod, f"{type(exc).__name__}: {exc}"))
    if failures:
        print("python-deps: FAIL", file=sys.stderr)
        for mod, msg in failures:
            print(f"  {mod}: {msg}", file=sys.stderr)
        return 1
    print(f"python-deps: OK ({len(MODULES)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
