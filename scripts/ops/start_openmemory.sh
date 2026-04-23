#!/usr/bin/env bash
# Start OpenMemory using local env files, preferring a nearby checkout and
# falling back to a globally installed `opm` when needed.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/ops/start_openmemory.sh [--openmemory-dir PATH] [--first-run]

Options:
  --openmemory-dir PATH  Explicit OpenMemory checkout dir (expects packages/openmemory-js)
  --first-run            Temporarily switch to migrator credentials and OM_PG_AUTO_DDL=true
  -h, --help             Show help

Notes:
  - Loads .env / .env.local via scripts/ops/load_env_local.sh
  - Resolution order: --openmemory-dir / OPENMEMORY_DIR, ../openmemory, common local dirs, global opm
  - Falls back to global `opm` when no runnable local checkout is found
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

OPENMEMORY_DIR="${OPENMEMORY_DIR:-}"
FIRST_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --openmemory-dir)
      if [ "$#" -lt 2 ]; then
        echo "[ERROR] --openmemory-dir requires a path" >&2
        exit 1
      fi
      OPENMEMORY_DIR="$2"
      shift 2
      ;;
    --first-run)
      FIRST_RUN=1
      shift
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

cd "${REPO_ROOT}"
source "${REPO_ROOT}/scripts/ops/load_env_local.sh"

resolve_openmemory_dir() {
  # Explicit path always wins when it exists.
  if [ -n "${OPENMEMORY_DIR}" ] && [ -d "${OPENMEMORY_DIR}" ]; then
    printf '%s\n' "${OPENMEMORY_DIR}"
    return 0
  fi

  # Prefer sibling checkout, then try common local clones.
  local candidates=(
    "${REPO_ROOT}/../openmemory/packages/openmemory-js"
    "${HOME}/openmemory/packages/openmemory-js"
    "${HOME}/Documents/openmemory/packages/openmemory-js"
    "${HOME}/Documents/ai/openmemory/packages/openmemory-js"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -d "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

openmemory_dir="$(resolve_openmemory_dir || true)"
node_cmd="$(command -v node || true)"
opm_cmd="$(command -v opm || true)"

if [ "${FIRST_RUN}" = "1" ]; then
  if [ -z "${OPENMEMORY_MIGRATOR_PASSWORD:-}" ] && [ -z "${OM_PG_PASSWORD:-}" ]; then
    echo "[ERROR] 需要 OPENMEMORY_MIGRATOR_PASSWORD 或 OM_PG_PASSWORD 才能首次启动 OpenMemory" >&2
    exit 1
  fi
  export OM_PG_USER="openmemory_migrator_login"
  if [ -n "${OPENMEMORY_MIGRATOR_PASSWORD:-}" ]; then
    export OM_PG_PASSWORD="${OPENMEMORY_MIGRATOR_PASSWORD}"
  fi
  export OM_PG_AUTO_DDL="true"
fi

echo "[INFO] Starting OpenMemory..."
echo "       port=${OM_PORT:-8080}"
echo "       base_url=${OPENMEMORY_BASE_URL:-http://127.0.0.1:${OM_PORT:-8080}}"
if [ "${FIRST_RUN}" = "1" ]; then
  echo "       mode=first-run (migrator + auto-ddl)"
fi

if [ -n "${openmemory_dir}" ]; then
  echo "       dir=${openmemory_dir}"
else
  echo "       dir=<auto>"
fi

if [ -n "${openmemory_dir}" ] \
  && [ -n "${node_cmd}" ] \
  && [ -f "${openmemory_dir}/bin/opm.js" ] \
  && [ -f "${openmemory_dir}/dist/server/index.js" ]; then
  if [ -n "${opm_cmd}" ]; then
    echo "[WARN] 检测到全局 opm: ${opm_cmd}" >&2
    echo "       当前将优先直接运行本地 checkout，避免误用旧版本。" >&2
  fi
  cd "${openmemory_dir}"
  exec "${node_cmd}" bin/opm.js serve
fi

if [ -n "${openmemory_dir}" ]; then
  if [ -z "${node_cmd}" ]; then
    echo "[WARN] 本地 openmemory 可见，但未找到 node；将尝试回退到全局 opm。" >&2
  elif [ ! -f "${openmemory_dir}/bin/opm.js" ]; then
    echo "[WARN] 本地 openmemory 缺少 ${openmemory_dir}/bin/opm.js；将尝试回退到全局 opm。" >&2
  elif [ ! -f "${openmemory_dir}/dist/server/index.js" ]; then
    echo "[WARN] 本地 openmemory 缺少编译产物 ${openmemory_dir}/dist/server/index.js；将尝试回退到全局 opm。" >&2
    echo "       如需优先使用源码目录，请先在该目录执行: npm install && npm run build" >&2
  fi
fi

if [ -n "${opm_cmd}" ]; then
  echo "       runtime=global-opm" >&2
  if [ -n "${openmemory_dir}" ] && [ -d "${openmemory_dir}" ]; then
    cd "${openmemory_dir}"
  fi
  exec "${opm_cmd}" serve
fi

echo "[ERROR] 未找到可启动的 OpenMemory 运行方式" >&2
echo "        已按顺序检查：指定目录、同级 ../openmemory、常见本地目录、全局 opm" >&2
echo "        可选方案：" >&2
echo "          1. 将可运行的 OpenMemory checkout 放到 ../openmemory/packages/openmemory-js" >&2
echo "          2. 通过 --openmemory-dir /path/to/openmemory/packages/openmemory-js 指定目录" >&2
echo "          3. 安装全局 OpenMemory CLI（npm install -g openmemory-js）" >&2
exit 1
