#!/usr/bin/env bash
# Build a single-file zipapp: dist/ptt-dictation.pyz
# Tiny (~tens of KB), runs on any Linux with python3 + the runtime deps that
# ./install.sh installs the deps into .venv; this just packages the source.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
rm -rf build/pyz
mkdir -p build/pyz dist
cp -r ptt_dictation build/pyz/
find build/pyz -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

python3 -m zipapp build/pyz \
    --output dist/ptt-dictation.pyz \
    --main "ptt_dictation.app:main" \
    --python "/usr/bin/env python3"
chmod +x dist/ptt-dictation.pyz

echo "Built dist/ptt-dictation.pyz ($(du -h dist/ptt-dictation.pyz | cut -f1))"
echo "Run:  ./dist/ptt-dictation.pyz        (or  ./dist/ptt-dictation.pyz --settings)"
