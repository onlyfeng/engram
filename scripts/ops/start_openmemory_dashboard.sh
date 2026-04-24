#!/usr/bin/env bash
# Start the OpenMemory Dashboard (Next.js dev server), preferring a nearby
# checkout and using env files from the Engram repo.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/ops/start_openmemory_dashboard.sh [--openmemory-dir PATH] [--port PORT]

Options:
  --openmemory-dir PATH  OpenMemory repo root (dashboard expected at PATH/dashboard)
                         Also accepts a direct path to the dashboard directory.
  --port PORT            Dashboard port (default: 3000)
  -h, --help             Show help

Notes:
  - Loads .env / .env.local via scripts/ops/load_env_local.sh
  - Resolution order: --openmemory-dir / OPENMEMORY_DASHBOARD_DIR, ../openmemory/dashboard,
    common local dirs, then fails with guidance.
  - NEXT_PUBLIC_API_URL defaults to http://localhost:<OM_PORT|8080> when not set.
  - OPENMEMORY_DASHBOARD_PORT=<PORT> has the same effect as --port.
EOF
}

_script="${BASH_SOURCE[0]}"
while [ -L "${_script}" ]; do
  _link_dir="$(cd "$(dirname "${_script}")" && pwd)"
  _script="$(readlink "${_script}")"
  case "${_script}" in
    /*) ;;
    *) _script="${_link_dir}/${_script}" ;;
  esac
done
SCRIPT_DIR="$(cd "$(dirname "${_script}")" && pwd)"
unset _script _link_dir

REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

for _arg in "$@"; do
  case "${_arg}" in -h|--help) usage; exit 0 ;; esac
done
unset _arg

_CALLER_DIR="$(pwd)"

# Normalize a caller-set OPENMEMORY_DASHBOARD_DIR before we cd away.
if [ -n "${OPENMEMORY_DASHBOARD_DIR:-}" ]; then
  case "${OPENMEMORY_DASHBOARD_DIR}" in
    /*) _CALLER_DASHBOARD_DIR="${OPENMEMORY_DASHBOARD_DIR}" ;;
    *)  _CALLER_DASHBOARD_DIR="${_CALLER_DIR}/${OPENMEMORY_DASHBOARD_DIR}" ;;
  esac
else
  _CALLER_DASHBOARD_DIR=""
fi
_CALLER_DASHBOARD_PORT="${OPENMEMORY_DASHBOARD_PORT:-}"

cd "${REPO_ROOT}"
source "${REPO_ROOT}/scripts/ops/load_env_local.sh"

# Restore caller overrides so they win over env files.
if [ -n "${_CALLER_DASHBOARD_DIR}" ]; then
  OPENMEMORY_DASHBOARD_DIR="${_CALLER_DASHBOARD_DIR}"
fi
if [ -n "${_CALLER_DASHBOARD_PORT}" ]; then
  OPENMEMORY_DASHBOARD_PORT="${_CALLER_DASHBOARD_PORT}"
fi
unset _CALLER_DASHBOARD_DIR _CALLER_DASHBOARD_PORT

DASHBOARD_DIR="${OPENMEMORY_DASHBOARD_DIR:-}"
PORT="${OPENMEMORY_DASHBOARD_PORT:-3000}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --openmemory-dir)
      if [ "$#" -lt 2 ]; then
        echo "[ERROR] --openmemory-dir requires a path" >&2
        exit 1
      fi
      case "$2" in
        /*) _raw_dir="$2" ;;
        *)  _raw_dir="${_CALLER_DIR}/$2" ;;
      esac
      # Accept either the repo root (contains dashboard/) or the dashboard dir directly.
      if [ -d "${_raw_dir}/dashboard" ]; then
        DASHBOARD_DIR="${_raw_dir}/dashboard"
      else
        DASHBOARD_DIR="${_raw_dir}"
      fi
      unset _raw_dir
      shift 2
      ;;
    --port)
      if [ "$#" -lt 2 ]; then
        echo "[ERROR] --port requires a value" >&2
        exit 1
      fi
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

unset _CALLER_DIR

# Fail fast when an explicit directory was given but does not exist.
if [ -n "${DASHBOARD_DIR}" ] && [ ! -d "${DASHBOARD_DIR}" ]; then
  echo "[ERROR] 指定的 Dashboard 目录不存在: ${DASHBOARD_DIR}" >&2
  exit 1
fi

resolve_dashboard_dir() {
  if [ -n "${DASHBOARD_DIR}" ] && [ -d "${DASHBOARD_DIR}" ]; then
    printf '%s\n' "${DASHBOARD_DIR}"
    return 0
  fi

  local candidates=()
  candidates+=("${REPO_ROOT}/../openmemory/dashboard")
  if [ -n "${HOME:-}" ]; then
    candidates+=(
      "${HOME}/openmemory/dashboard"
      "${HOME}/Documents/openmemory/dashboard"
      "${HOME}/Documents/ai/openmemory/dashboard"
    )
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    [ -d "${candidate}" ] || continue
    if [ -f "${candidate}/package.json" ] && [ -f "${candidate}/node_modules/.bin/next" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
    if [ -f "${candidate}/package.json" ]; then
      echo "[WARN] 发现 ${candidate} 但缺少 node_modules，跳过继续搜索。" >&2
      echo "       如需使用该目录，请先执行: npm install" >&2
    fi
  done
  return 1
}

dashboard_dir="$(resolve_dashboard_dir || true)"

if [ -z "${dashboard_dir}" ]; then
  echo "[ERROR] 未找到可启动的 OpenMemory Dashboard" >&2
  echo "        已按顺序检查：指定目录、../openmemory/dashboard、常见本地目录" >&2
  echo "        可选方案：" >&2
  echo "          1. 将 OpenMemory checkout 放到 ../openmemory" >&2
  echo "          2. 通过 --openmemory-dir /path/to/openmemory 指定目录" >&2
  echo "          3. 在 dashboard 目录执行 npm install 安装依赖" >&2
  exit 1
fi

# Default NEXT_PUBLIC_API_URL to the OpenMemory backend if not already set.
if [ -z "${NEXT_PUBLIC_API_URL:-}" ]; then
  export NEXT_PUBLIC_API_URL="http://localhost:${OM_PORT:-8080}"
fi

echo "[INFO] Starting OpenMemory Dashboard..."
echo "       dir=${dashboard_dir}"
echo "       port=${PORT}"
echo "       api=${NEXT_PUBLIC_API_URL}"

cd "${dashboard_dir}"
exec npm run dev -- --port "${PORT}"
