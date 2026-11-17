#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$HARU_PROJECT_ROOT"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "This local runtime requires Apple Silicon macOS." >&2; exit 1
fi
command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/" >&2; exit 1; }
mkdir -p .runtime
REVISION="$(/usr/bin/python3 -c 'import json; print(json.load(open("runtime.json"))["upstream_revision"])')"
if [[ ! -d .runtime/Haru/.git ]]; then
    git clone https://github.com/higurashi-ai/Haru.git .runtime/Haru
fi
if [[ -n "$(git -C .runtime/Haru status --porcelain)" ]]; then
    echo "Upstream checkout has local changes; preserve them before setup." >&2; exit 1
fi
if [[ "$(git -C .runtime/Haru rev-parse HEAD)" != "$REVISION" ]]; then
    git -C .runtime/Haru fetch origin "$REVISION"
    git -C .runtime/Haru checkout --detach "$REVISION"
fi
uv python install --no-bin "$(cat .python-version)"
uv sync --locked
.venv/bin/python -c 'import mlx.core as mx; import haru_mlx; print("MLX", mx.__version__, "Metal available:", mx.metal.is_available()); assert mx.metal.is_available()'
echo "Ready. Run ./scripts/haru download, then ./scripts/haru smoke-ko."
