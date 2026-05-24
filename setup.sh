#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APT_UPDATED=0

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

log() {
  printf '[setup] %s\n' "$1"
}

warn() {
  printf '[setup][warn] %s\n' "$1"
}

err() {
  printf '[setup][error] %s\n' "$1"
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
      yay -S --needed --noconfirm "$pkg"
      ;;
    pacman)
      sudo pacman -S --needed --noconfirm "$pkg"
      ;;
    apt-get)
      if [[ "$APT_UPDATED" -eq 0 ]]; then
        sudo apt-get update -y
        APT_UPDATED=1
      fi
      sudo apt-get install -y "$pkg"
      ;;
    dnf)
      sudo dnf install -y "$pkg"
      ;;
    zypper)
      sudo zypper --non-interactive install "$pkg"
      ;;
    *)
      return 1
      ;;
  esac
}

pkg_for_installer() {
  local installer="$1"
  local logical="$2"
  case "$logical" in
    python3)
      case "$installer" in
        apt-get|dnf|zypper|pacman|yay) echo "python3" ;;
        *) echo "python3" ;;
      esac
      ;;
    node)
      case "$installer" in
        apt-get|dnf|zypper|pacman|yay) echo "nodejs" ;;
        *) echo "nodejs" ;;
      esac
      ;;
    npm)
      case "$installer" in
        apt-get|dnf|zypper|pacman|yay) echo "npm" ;;
        *) echo "npm" ;;
      esac
      ;;
    git|curl)
      echo "$logical"
      ;;
    gh)
      case "$installer" in
        apt-get) echo "gh" ;;
        dnf) echo "gh" ;;
        zypper) echo "gh" ;;
        pacman|yay) echo "github-cli" ;;
        *) echo "gh" ;;
      esac
      ;;
    *)
      echo "$logical"
      ;;
  esac
}

ensure_core_cmd() {
  local cmd="$1"
  local logical_pkg="$2"
  local installer="$3"
  if has_cmd "$cmd"; then
    log "$cmd found"
    return 0
  fi
  if [[ -z "$installer" ]]; then
    err "$cmd missing and no supported package manager detected"
    return 1
  fi
  local pkg
  pkg="$(pkg_for_installer "$installer" "$logical_pkg")"
  log "$cmd missing; installing $pkg via $installer"
  if ! install_pkg "$installer" "$pkg"; then
    err "failed to install $pkg for $cmd"
    return 1
  fi
  if has_cmd "$cmd"; then
    log "$cmd installed"
    return 0
  fi
  err "$cmd still missing after install attempt"
  return 1
}

try_npm_install() {
  local cmd="$1"
  shift
  local packages=("$@")

  if has_cmd "$cmd"; then
    log "$cmd found"
    return 0
  fi
  if ! has_cmd npm; then
    warn "$cmd missing and npm unavailable; cannot auto-install"
    return 1
  fi

  local pkg
  for pkg in "${packages[@]}"; do
    log "Attempting npm install for $cmd via package: $pkg"
    if npm install -g "$pkg"; then
      if has_cmd "$cmd"; then
        log "$cmd installed via $pkg"
        return 0
      fi
    fi
  done
  warn "Unable to auto-install $cmd via npm candidates"
  return 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  log "Non-Linux platform detected. Running configure directly."
  exec python3 "${SCRIPT_DIR}/scripts/llm_council.py" configure "$@"
fi

INSTALLER="$(detect_installer)"
if [[ -z "$INSTALLER" ]]; then
  warn "No supported package manager detected."
else
  log "Using installer: $INSTALLER"
fi

core_failures=0
ensure_core_cmd python3 python3 "$INSTALLER" || core_failures=$((core_failures + 1))
ensure_core_cmd git git "$INSTALLER" || core_failures=$((core_failures + 1))
ensure_core_cmd curl curl "$INSTALLER" || core_failures=$((core_failures + 1))
ensure_core_cmd node node "$INSTALLER" || core_failures=$((core_failures + 1))
ensure_core_cmd npm npm "$INSTALLER" || core_failures=$((core_failures + 1))

if [[ "$core_failures" -gt 0 ]]; then
  err "Critical dependencies missing ($core_failures). Fix errors and re-run setup."
  exit 1
fi

# Optional but recommended: GitHub CLI for auth/token workflows.
if [[ -n "$INSTALLER" ]] && ! has_cmd gh; then
  log "Attempting to install GitHub CLI (gh)"
  GH_PKG="$(pkg_for_installer "$INSTALLER" gh)"
  install_pkg "$INSTALLER" "$GH_PKG" || warn "Could not install gh automatically"
fi

# Agent CLIs (Linux): try official npm package names first.
agent_failures=0
try_npm_install codex "@openai/codex" || agent_failures=$((agent_failures + 1))
try_npm_install claude "@anthropic-ai/claude-code" || agent_failures=$((agent_failures + 1))
try_npm_install gemini "@google/gemini-cli" || agent_failures=$((agent_failures + 1))
try_npm_install opencode "opencode-ai" "@opencode-ai/cli" || agent_failures=$((agent_failures + 1))

if [[ "$agent_failures" -gt 0 ]]; then
  warn "$agent_failures agent CLI tool(s) are still missing. You can continue with configure and use available/custom agents."
fi

log "Starting interactive council configuration..."
exec python3 "${SCRIPT_DIR}/scripts/llm_council.py" configure "$@"
