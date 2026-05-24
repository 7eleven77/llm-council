#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

log() {
  printf '[setup] %s\n' "$1"
}

detect_installer() {
  if has_cmd yay; then
    echo "yay"
    return
  fi
  if has_cmd pacman; then
    echo "pacman"
    return
  fi
  if has_cmd apt-get; then
    echo "apt-get"
    return
  fi
  if has_cmd dnf; then
    echo "dnf"
    return
  fi
  if has_cmd zypper; then
    echo "zypper"
    return
  fi
  echo ""
}

install_pkg() {
  local installer="$1"
  local pkg="$2"
  case "$installer" in
    yay)
      yay -S --needed --noconfirm "$pkg" || true
      ;;
    pacman)
      sudo pacman -S --needed --noconfirm "$pkg" || true
      ;;
    apt-get)
      sudo apt-get update -y || true
      sudo apt-get install -y "$pkg" || true
      ;;
    dnf)
      sudo dnf install -y "$pkg" || true
      ;;
    zypper)
      sudo zypper --non-interactive install "$pkg" || true
      ;;
  esac
}

ensure_cmd() {
  local cmd="$1"
  local pkg="$2"
  local installer="$3"
  if has_cmd "$cmd"; then
    log "$cmd found"
    return
  fi
  log "$cmd missing; attempting install via $installer ($pkg)"
  if [[ -z "$installer" ]]; then
    log "No supported package manager detected. Install $pkg manually."
    return
  fi
  install_pkg "$installer" "$pkg"
  if has_cmd "$cmd"; then
    log "$cmd installed"
  else
    log "$cmd still missing after install attempt"
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  log "Non-Linux platform detected. Running configure directly."
  exec python3 "${SCRIPT_DIR}/scripts/llm_council.py" configure "$@"
fi

INSTALLER="$(detect_installer)"
if [[ -z "$INSTALLER" ]]; then
  log "No supported package manager detected; dependency install may be incomplete."
else
  log "Using installer: $INSTALLER"
fi

# Core requirements
ensure_cmd python3 python "$INSTALLER"
ensure_cmd git git "$INSTALLER"
ensure_cmd curl curl "$INSTALLER"
ensure_cmd node nodejs "$INSTALLER"
ensure_cmd npm npm "$INSTALLER"

# Recommended ecosystem tools
ensure_cmd gh github-cli "$INSTALLER"
ensure_cmd codex codex "$INSTALLER"
ensure_cmd claude claude "$INSTALLER"
ensure_cmd gemini gemini "$INSTALLER"
ensure_cmd opencode opencode "$INSTALLER"

log "Starting interactive council configuration..."
exec python3 "${SCRIPT_DIR}/scripts/llm_council.py" configure "$@"
