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
  --port PORT            Dashboard port 1-65535 (default: 3000)
  -h, --help             Show help

Notes:
  - Loads .env / .env.local via scripts/ops/load_env_local.sh
  - Resolution order: --openmemory-dir / OPENMEMORY_DASHBOARD_DIR, OPENMEMORY_DIR-derived
    dashboard, ../openmemory/dashboard, common local dirs, then fails with guidance.
  - NEXT_PUBLIC_API_URL defaults to OPENMEMORY_BASE_URL, then http://localhost:<OM_PORT|8080>.
  - NEXT_PUBLIC_API_KEY defaults to OM_API_KEY when set, so authenticated local APIs work.
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
# Use ${var+x} to distinguish "set to empty" from "unset", so a caller that
# explicitly exports OPENMEMORY_DASHBOARD_DIR="" can clear env-file values.
if [ -n "${OPENMEMORY_DASHBOARD_DIR+x}" ]; then
  _CALLER_DASHBOARD_DIR_WAS_SET=1
  if [ -n "${OPENMEMORY_DASHBOARD_DIR:-}" ]; then
    case "${OPENMEMORY_DASHBOARD_DIR}" in
      /*) _CALLER_DASHBOARD_DIR="${OPENMEMORY_DASHBOARD_DIR}" ;;
      *)  _CALLER_DASHBOARD_DIR="${_CALLER_DIR}/${OPENMEMORY_DASHBOARD_DIR}" ;;
    esac
  else
    _CALLER_DASHBOARD_DIR=""
  fi
else
  _CALLER_DASHBOARD_DIR_WAS_SET=0
  _CALLER_DASHBOARD_DIR=""
fi
_CALLER_DASHBOARD_PORT="${OPENMEMORY_DASHBOARD_PORT:-}"
_CALLER_NEXT_PUBLIC_API_KEY_WAS_SET=0
_CALLER_NEXT_PUBLIC_API_KEY=""
if [ -n "${NEXT_PUBLIC_API_KEY+x}" ]; then
  _CALLER_NEXT_PUBLIC_API_KEY_WAS_SET=1
  _CALLER_NEXT_PUBLIC_API_KEY="${NEXT_PUBLIC_API_KEY:-}"
fi
# Normalize a caller-set OPENMEMORY_DIR before we cd away.
if [ -n "${OPENMEMORY_DIR:-}" ]; then
  case "${OPENMEMORY_DIR}" in
    /*) _CALLER_OM_DIR="${OPENMEMORY_DIR}" ;;
    *)  _CALLER_OM_DIR="${_CALLER_DIR}/${OPENMEMORY_DIR}" ;;
  esac
else
  _CALLER_OM_DIR=""
fi

cd "${REPO_ROOT}"
source "${REPO_ROOT}/scripts/ops/load_env_local.sh"

# Restore caller overrides so they win over env files.
# Use the WAS_SET flag so an explicit empty value also overrides env-file content.
if [ "${_CALLER_DASHBOARD_DIR_WAS_SET}" = "1" ]; then
  OPENMEMORY_DASHBOARD_DIR="${_CALLER_DASHBOARD_DIR}"
fi
if [ -n "${_CALLER_DASHBOARD_PORT}" ]; then
  OPENMEMORY_DASHBOARD_PORT="${_CALLER_DASHBOARD_PORT}"
fi
if [ "${_CALLER_NEXT_PUBLIC_API_KEY_WAS_SET}" = "1" ]; then
  NEXT_PUBLIC_API_KEY="${_CALLER_NEXT_PUBLIC_API_KEY}"
fi
if [ -n "${_CALLER_OM_DIR}" ]; then
  OPENMEMORY_DIR="${_CALLER_OM_DIR}"
fi
# If caller supplied OPENMEMORY_DIR but not OPENMEMORY_DASHBOARD_DIR, prevent
# an env-file OPENMEMORY_DASHBOARD_DIR from silently winning over the caller's intent.
if [ -n "${_CALLER_OM_DIR}" ] && [ "${_CALLER_DASHBOARD_DIR_WAS_SET}" = "0" ]; then
  OPENMEMORY_DASHBOARD_DIR=""
fi
unset _CALLER_DASHBOARD_DIR _CALLER_DASHBOARD_PORT _CALLER_OM_DIR _CALLER_DASHBOARD_DIR_WAS_SET _CALLER_NEXT_PUBLIC_API_KEY

dashboard_from_openmemory_dir() {
  local om_dir="$1"
  [ -n "${om_dir}" ] || return 1
  [ -d "${om_dir}" ] || return 1
  if [ -d "${om_dir}/dashboard" ]; then
    printf '%s\n' "${om_dir}/dashboard"
    return 0
  fi
  # OPENMEMORY_DIR normally points at packages/openmemory-js for the backend launcher.
  # Only probe the sibling dashboard when the path is exactly .../packages/openmemory-js.
  if [ "$(basename "${om_dir}")" = "openmemory-js" ] \
     && [ "$(basename "$(dirname "${om_dir}")")" = "packages" ] \
     && [ -d "${om_dir}/../../dashboard" ]; then
    local repo_root
    repo_root="$(cd "${om_dir}/../.." && pwd)"
    printf '%s\n' "${repo_root}/dashboard"
    return 0
  fi
  return 1
}

DASHBOARD_DIR="${OPENMEMORY_DASHBOARD_DIR:-}"
# If OPENMEMORY_DASHBOARD_DIR is unset but OPENMEMORY_DIR is configured (e.g. via .env.local),
# derive dashboard/ from either an OpenMemory repo root or packages/openmemory-js path.
if [ -z "${DASHBOARD_DIR}" ] && [ -n "${OPENMEMORY_DIR:-}" ]; then
  DASHBOARD_DIR="$(dashboard_from_openmemory_dir "${OPENMEMORY_DIR}" || true)"
fi
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
      # Accept the repo root, packages/openmemory-js, or the dashboard dir directly.
      if [ -d "${_raw_dir}/dashboard" ]; then
        DASHBOARD_DIR="${_raw_dir}/dashboard"
      elif [ "$(basename "${_raw_dir}")" = "openmemory-js" ] \
           && [ "$(basename "$(dirname "${_raw_dir}")")" = "packages" ] \
           && [ -d "${_raw_dir}/../../dashboard" ]; then
        _repo_root="$(cd -P "${_raw_dir}/../.." && pwd -P)"
        DASHBOARD_DIR="${_repo_root}/dashboard"
        unset _repo_root
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

# Validate port is a number in range 1-65535.
# Reject non-digits and 6+ digit strings before the arithmetic comparison so that
# very large integers cannot overflow the shell's integer type.
case "${PORT}" in
  ''|*[!0-9]*|??????*)
    echo "[ERROR] --port 必须是 1-65535 之间的整数，得到: '${PORT}'" >&2
    exit 1
    ;;
esac
if [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; then
  echo "[ERROR] --port 必须是 1-65535 之间的整数，得到: ${PORT}" >&2
  exit 1
fi

# Fail fast when an explicit directory was given but does not exist.
if [ -n "${DASHBOARD_DIR}" ] && [ ! -d "${DASHBOARD_DIR}" ]; then
  echo "[ERROR] 指定的 Dashboard 目录不存在: ${DASHBOARD_DIR}" >&2
  exit 1
fi

# Fail fast when an explicit directory exists but is not a Node.js project.
if [ -n "${DASHBOARD_DIR}" ] && [ -d "${DASHBOARD_DIR}" ] && [ ! -f "${DASHBOARD_DIR}/package.json" ]; then
  echo "[ERROR] 指定目录缺少 package.json: ${DASHBOARD_DIR}" >&2
  echo "        请传入 OpenMemory repo root 或包含 package.json 的 dashboard 目录。" >&2
  echo "        若依赖尚未安装，请在 dashboard 目录执行: npm install" >&2
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

# Check runtime dependencies before attempting to start.
node_cmd="$(command -v node || true)"
npm_cmd="$(command -v npm || true)"
if [ -z "${node_cmd}" ]; then
  echo "[ERROR] 未找到 node 命令，请安装 Node.js 并确认其在 PATH 中。" >&2
  exit 1
fi
if [ -z "${npm_cmd}" ]; then
  echo "[ERROR] 未找到 npm 命令，请安装 Node.js/npm 并确认其在 PATH 中。" >&2
  exit 1
fi

# Default NEXT_PUBLIC_API_URL: prefer explicit OPENMEMORY_BASE_URL, then localhost.
if [ -z "${NEXT_PUBLIC_API_URL:-}" ]; then
  if [ -n "${OPENMEMORY_BASE_URL:-}" ]; then
    export NEXT_PUBLIC_API_URL="${OPENMEMORY_BASE_URL}"
  else
    export NEXT_PUBLIC_API_URL="http://localhost:${OM_PORT:-8080}"
  fi
fi
# The dashboard runs API calls in the browser and reads only NEXT_PUBLIC_API_KEY.
# Reuse the local OpenMemory key by default; do not print it in startup logs.
if [ -z "${NEXT_PUBLIC_API_KEY:-}" ] \
   && [ "${_CALLER_NEXT_PUBLIC_API_KEY_WAS_SET}" = "0" ] \
   && [ -n "${OM_API_KEY:-}" ]; then
  export NEXT_PUBLIC_API_KEY="${OM_API_KEY}"
fi
unset _CALLER_NEXT_PUBLIC_API_KEY_WAS_SET

echo "[INFO] Starting OpenMemory Dashboard..."
echo "       dir=${dashboard_dir}"
echo "       port=${PORT}"
echo "       api=${NEXT_PUBLIC_API_URL}"

cd "${dashboard_dir}"
exec "${npm_cmd}" run dev -- --port "${PORT}"
