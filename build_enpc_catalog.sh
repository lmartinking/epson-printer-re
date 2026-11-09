#!/usr/bin/env bash
# Rebuild enpc_schema/ from EpsonNet Config V4's shipped template set.
#
# Reads the ENDU .dat/.ens templates from inside the installed macOS app bundle
# and writes decoded string dumps + CATALOG.tsv + ITEMS.tsv into ./enpc_schema/.
#
# Override the app path with EPSONNET_APP=/path/to/App.app if it's installed
# somewhere non-standard.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

: "${EPSONNET_APP:=/Applications/Epson Software/EpsonNet/EpsonNet Config V4/EpsonNet Config.app}"

TEMPLATES="${EPSONNET_APP}/Contents/Resources/Settings/TemplateSet/Default"
OUT="${REPO_ROOT}/enpc_schema"

if [ ! -d "$TEMPLATES" ]; then
  cat >&2 <<EOF
error: TemplateSet directory not found:
  $TEMPLATES

Install EpsonNet Config V4 (from Epson's downloads page), or point
EPSONNET_APP at the .app bundle if it's elsewhere:

  EPSONNET_APP=/path/to/EpsonNet\ Config.app $0
EOF
  exit 1
fi

echo "templates: $TEMPLATES"
echo "output:    $OUT"
mkdir -p "$OUT"
exec python3 "$REPO_ROOT/build_enpc_catalog.py" all "$TEMPLATES" "$OUT"
