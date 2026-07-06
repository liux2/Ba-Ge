#!/usr/bin/env bash
# Build a single-file zipapp: dist/ba-ge.pyz
# Tiny (~tens of KB), runs on any Linux with python3 + the runtime deps that
# ./install.sh installs the deps into .venv; this just packages the source.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
rm -rf build/pyz
mkdir -p build/pyz dist
cp -r ba_ge build/pyz/
find build/pyz -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

python3 -m zipapp build/pyz \
    --output dist/ba-ge.pyz \
    --main "ba_ge.app:main" \
    --python "/usr/bin/env python3"
chmod +x dist/ba-ge.pyz

echo "Built dist/ba-ge.pyz ($(du -h dist/ba-ge.pyz | cut -f1))"
echo "Run:  ./dist/ba-ge.pyz        (or  ./dist/ba-ge.pyz --settings)"
