#!/usr/bin/env bash
# Load .env and .env.local into current shell (source this script).

_is_sourced() {
  if [ -n "${BASH_SOURCE[0]-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
  fi
  case "${ZSH_EVAL_CONTEXT-}" in
    *:file) return 0 ;;
  esac
  return 1
}

if ! _is_sourced; then
  echo "[ERROR] 请使用: source scripts/ops/load_env_local.sh" >&2
  return 1 2>/dev/null || exit 1
fi

if [ -n "${ZSH_VERSION-}" ]; then
  SCRIPT_SOURCE="$(eval 'echo ${(%):-%x}')"
elif [ -n "${BASH_SOURCE[0]-}" ]; then
  SCRIPT_SOURCE="${BASH_SOURCE[0]}"
else
  SCRIPT_SOURCE="$0"
fi

SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT_DIR}" || return 1

ENV_LOCAL_FILE="${ENV_LOCAL_FILE:-.env.local}"
if [ "${ENV_LOCAL_FILE#/}" = "${ENV_LOCAL_FILE}" ]; then
  ENV_LOCAL_PATH="${ROOT_DIR}/${ENV_LOCAL_FILE}"
else
  ENV_LOCAL_PATH="${ENV_LOCAL_FILE}"
fi

loaded=()
set -a
if [ -f "${ROOT_DIR}/.env" ]; then
  . "${ROOT_DIR}/.env"
  loaded+=(".env")
fi
if [ -f "${ENV_LOCAL_PATH}" ]; then
  . "${ENV_LOCAL_PATH}"
  loaded+=("${ENV_LOCAL_PATH}")
fi
set +a

if [ "${#loaded[@]}" -eq 0 ]; then
  echo "[WARN] 未找到 .env 或 ${ENV_LOCAL_FILE}"
else
  echo "[OK] 已加载: ${loaded[*]}"
fi
