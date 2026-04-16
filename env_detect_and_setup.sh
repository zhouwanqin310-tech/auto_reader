#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$PROJECT_ROOT/requirements.txt"
CONFIG_FILE="$PROJECT_ROOT/config.yaml"
VENV_DIR="$PROJECT_ROOT/.venv"

MODE="${1:-setup}"

log() {
  printf '%s\n' "$*"
}

err() {
  printf 'ERROR: %s\n' "$*" >&2
}

detect_platform() {
  local uname_s=""
  uname_s="$(uname -s 2>/dev/null || echo unknown)"

  # Prefer env-based detection for WSL
  if [ -n "${WSL_DISTRO_NAME:-}" ]; then
    echo "wsl"
    return 0
  fi

  if [ -r /proc/version ]; then
    # Fallback WSL detection without grep
    if awk 'BEGIN{IGNORECASE=1} /microsoft/ {found=1} END{exit !found}' /proc/version 2>/dev/null; then
      echo "wsl"
      return 0
    fi
  fi

  case "$uname_s" in
    Linux) echo "linux" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows_gitbash" ;;
    *) echo "$uname_s" ;;
  esac
}

detect_python() {
  PY_ARGS=()
  if command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
  elif command -v py >/dev/null 2>&1; then
    # Windows launcher: `py -3`
    PY_BIN="py"
    PY_ARGS=(-3)
  else
    err "未找到 Python。请先安装 Python (建议 3.10+) 后再运行该脚本。"
    exit 1
  fi
}

activate_venv() {
  if [ -f "$VENV_DIR/bin/activate" ]; then
    # POSIX venv
    # shellcheck disable=SC1090
    . "$VENV_DIR/bin/activate"
  elif [ -f "$VENV_DIR/Scripts/activate" ]; then
    # Windows venv (Git Bash/WSL)
    # shellcheck disable=SC1090
    . "$VENV_DIR/Scripts/activate"
  else
    err "未找到虚拟环境激活脚本：$VENV_DIR"
    exit 1
  fi
}

usage() {
  printf '%s\n' \
    "用法：" \
    "  ./env_detect_and_setup.sh             # 仅完成装配（推荐）" \
    "  ./env_detect_and_setup.sh --start    # 装配后前台启动服务（阻塞）" \
    "" \
    "说明：" \
    "- 本脚本是 bash：在 Windows 上请使用 Git Bash 或 WSL 运行。" \
    "- 背景启动在不同环境差异较大；如需后台请直接用项目里的 start_web.sh。" \
    ""
}

main() {
  if [ "${MODE}" = "--help" ] || [ "${MODE}" = "-h" ]; then
    usage
    exit 0
  fi

  if [ "${MODE}" = "--start" ]; then
    MODE="start"
  else
    MODE="setup"
  fi

  PLATFORM="$(detect_platform)"
  log "检测平台：$PLATFORM"

  if [ ! -f "$REQ_FILE" ]; then
    err "未找到 requirements.txt：$REQ_FILE"
    exit 1
  fi
  if [ ! -f "$CONFIG_FILE" ]; then
    err "未找到 config.yaml：$CONFIG_FILE"
    exit 1
  fi

  # Detect python
  PY_BIN=""
  detect_python

  log "使用 Python：$PY_BIN ${PY_ARGS[*]-}"

  # Create venv
  if [ ! -d "$VENV_DIR" ]; then
    log "创建虚拟环境：$VENV_DIR"
    "$PY_BIN" "${PY_ARGS[@]}" -m venv "$VENV_DIR"
  else
    log "虚拟环境已存在：$VENV_DIR"
  fi

  activate_venv

  log "升级 pip 并安装依赖（不会输出 api_key）..."
  # shellcheck disable=SC2086
  "$PY_BIN" "${PY_ARGS[@]}" -m pip install -U pip setuptools wheel
  "$PY_BIN" "${PY_ARGS[@]}" -m pip install -r "$REQ_FILE"

  # Config validation (no key leak)
  log "校验配置字段 miniMax.api_key 是否存在/非空..."
  CONFIG_OK="$("$PY_BIN" "${PY_ARGS[@]}" - <<PY
import pathlib
try:
    import yaml
    cfg = pathlib.Path("$CONFIG_FILE")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    api_key = (data.get("miniMax", {}) or {}).get("api_key", "")
    print("OK" if isinstance(api_key, str) and api_key.strip() else "MISSING_OR_EMPTY")
except Exception:
    # Avoid printing stack traces (and any YAML snippets) that may include secrets.
    print("CONFIG_PARSE_FAILED")
PY
)"
  if [ "$CONFIG_OK" != "OK" ]; then
    if [ "$CONFIG_OK" = "CONFIG_PARSE_FAILED" ]; then
      err "config.yaml 解析失败。请检查 YAML 格式是否正确（注意不要把真实 api_key 打到终端/日志里）。"
    else
      err "config.yaml 中 miniMax.api_key 缺失或为空。请先填写你的 API Key。"
    fi
    exit 1
  fi

  # Best-effort protect config permissions on Linux/macOS
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true

  # Ensure storage dirs exist
  mkdir -p "$PROJECT_ROOT/papers/notes" "$PROJECT_ROOT/pdfs" "$PROJECT_ROOT/chat_history"

  log "装配完成。"

  if [ "$MODE" = "start" ]; then
    log "前台启动服务：python web/app.py （端口 5001）"
    exec "$PY_BIN" "${PY_ARGS[@]}" "$PROJECT_ROOT/web/app.py"
  fi
}

main "$@"

