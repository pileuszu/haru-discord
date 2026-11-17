#!/bin/bash
# Source this file from project scripts; caches stay inside this checkout.
HARU_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="$HARU_PROJECT_ROOT/.runtime/uv-cache"
export UV_PYTHON_INSTALL_DIR="$HARU_PROJECT_ROOT/.runtime/python"
export HF_HOME="$HARU_PROJECT_ROOT/.runtime/huggingface"
export HF_HUB_DISABLE_TELEMETRY=1
export PYTHONUNBUFFERED=1
