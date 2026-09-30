#!/usr/bin/env bash
# Run the full test suites from a bare clone of this repository.
#
# Four guide tests import `gwdc_tg_adapter` (the Telegram adapter that runs inside the owner's private
# bot). That module imports this repository as the package `AI_CONTEST.gwdc_2026.guide`, and the tests
# locate the adapter via `Path(__file__).resolve().parents[3]`, so this script recreates that layout in
# a fresh temporary directory (a real copy, not a symlink, because the tests resolve symlinks):
#   <tmp>/gwdc_tg_adapter.py            <- copy of hani_bot_hook/gwdc_tg_adapter.py (byte-identical)
#   <tmp>/AI_CONTEST/gwdc_2026/         <- copy of this clone (guide/, safebatch/, hani_bot_hook/ ...)
# and points SB_GUIDE_DIR at the copy's guide/. Tests create their own tempdirs for state files; nothing
# outside the clone and <tmp> is read or written. No test is skipped or removed. <tmp> is deleted at exit.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/safebatch_tests.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/AI_CONTEST/gwdc_2026"
# copy the whole clone (some tests read recorded evidence under prep_20260914/), skipping .git and any venv
for e in "$ROOT"/* "$ROOT"/.[!.]*; do
  [ -e "$e" ] || continue
  n="$(basename "$e")"
  case "$n" in .git|.venv|venv|__pycache__|.env|.env.*|.DS_Store) continue;; esac   # never copy a local .env (secrets) into the layout
  cp -R "$e" "$TMP/AI_CONTEST/gwdc_2026/$n"
done
cp "$ROOT/hani_bot_hook/gwdc_tg_adapter.py" "$TMP/gwdc_tg_adapter.py"
export SB_GUIDE_DIR="$TMP/AI_CONTEST/gwdc_2026/guide"
export PYTHONDONTWRITEBYTECODE=1
echo "python: $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
echo "layout: $TMP/AI_CONTEST/gwdc_2026 (copy of $ROOT)"
echo "== module load paths =="
( cd "$TMP/AI_CONTEST/gwdc_2026/guide/tests" && "$PY" - <<'EOF'
import sys, pathlib
here = pathlib.Path.cwd()
sys.path[:0] = [str(here), str(here.parent), str(here.parents[3])]
import gwdc_tg_adapter, phone_chat, phone_intent, phone_signer, phone_flow
for m in (gwdc_tg_adapter, phone_chat, phone_intent, phone_signer, phone_flow):
    print(f"  {m.__name__:18s} {m.__file__}")
print(f"  {'GUIDE (adapter)':18s} {gwdc_tg_adapter.GUIDE}")
EOF
)
echo "== guide tests =="
( cd "$TMP/AI_CONTEST/gwdc_2026/guide" && "$PY" -m unittest discover -s tests )
echo "== safebatch tests =="
( cd "$TMP/AI_CONTEST/gwdc_2026/safebatch" && "$PY" -m unittest discover -s tests )
