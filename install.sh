#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT_PATH=""
if [[ -f "${BASH_SOURCE[0]}" ]]; then
  INSTALL_SCRIPT_PATH="$SOURCE_DIR/$(basename "${BASH_SOURCE[0]}")"
fi
DEFAULT_INSTALL_DIR="$HOME/.local/share/ollama-document-assistant"
PROJECT_DIR="$DEFAULT_INSTALL_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_SYSTEMD=1
START_SERVICE=1
INSTALL_SYSTEM_DEPS=0
INSTALL_OLLAMA=0
PULL_MODELS=0
MODEL_OVERRIDE=""
REPO_URL="https://github.com/maheis/ollama-document-assistant.git"
REPO_REF=""

print_usage() {
  cat <<'USAGE'
Usage: bash ./install.sh [options]

Options:
  --full-setup         Enable: --install-system-deps --install-ollama --pull-models
  --install-system-deps
                       Install required Debian/Ubuntu packages via apt-get
  --install-ollama     Install Ollama and try to start/enable service
  --pull-models        Pull model(s) in Ollama (default: qwen2.5:7b-instruct)
  --model <name>       Model name to pull (repeatable by comma: m1,m2)
  --install-dir <dir>  Installation directory (default: ~/.local/share/ollama-document-assistant)
  --repo-url <url>     Git repository URL for bootstrap checkout
  --repo-ref <ref>     Optional git branch/tag/commit to checkout
  --no-systemd         Do not install user systemd unit
  --no-start           Install unit but do not start/restart it
  --help               Show this help

Examples:
  bash ./install.sh
  bash ./install.sh --full-setup
  bash ./install.sh --install-system-deps --install-ollama --pull-models --model qwen2.5:7b-instruct
  curl -fsSL https://raw.githubusercontent.com/maheis/ollama-document-assistant/main/install.sh | bash -s -- --full-setup
USAGE
}

run_as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
    return
  fi
  echo "[ERROR] Root privileges required for: $* (sudo not found)" >&2
  exit 2
}

cmd_exists() {
  command -v "$1" >/dev/null 2>&1
}

has_project_sources() {
  local dir="$1"
  [[ -f "$dir/organize.py" ]] &&
    [[ -f "$dir/review_web.py" ]] &&
    [[ -f "$dir/doc_assistant_service.py" ]] &&
    [[ -f "$dir/assistant_config.py" ]] &&
    [[ -f "$dir/requirements.txt" ]]
}

ensure_project_checkout() {
  if has_project_sources "$SOURCE_DIR"; then
    return
  fi

  if has_project_sources "$PROJECT_DIR"; then
    SOURCE_DIR="$PROJECT_DIR"
    return
  fi

  if ! cmd_exists git; then
    echo "[ERROR] git is required for bootstrap checkout from --repo-url" >&2
    exit 2
  fi

  if [[ -d "$PROJECT_DIR/.git" ]]; then
    echo "Updating repository in $PROJECT_DIR"
    (
      cd "$PROJECT_DIR"
      git fetch --all --tags --prune
      if [[ -n "$REPO_REF" ]]; then
        git checkout "$REPO_REF"
      fi
      git pull --ff-only || true
    )
  else
    if [[ -d "$PROJECT_DIR" ]] && [[ -n "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]]; then
      echo "[ERROR] Install directory is not empty and not a git repo: $PROJECT_DIR" >&2
      echo "        Choose another --install-dir or clean the directory." >&2
      exit 2
    fi
    rm -rf "$PROJECT_DIR"
    git clone "$REPO_URL" "$PROJECT_DIR"
    if [[ -n "$REPO_REF" ]]; then
      (cd "$PROJECT_DIR" && git checkout "$REPO_REF")
    fi
  fi

  if ! has_project_sources "$PROJECT_DIR"; then
    echo "[ERROR] Repository checkout incomplete in $PROJECT_DIR" >&2
    exit 2
  fi
  SOURCE_DIR="$PROJECT_DIR"
}

sync_project_files() {
  if [[ "$SOURCE_DIR" == "$PROJECT_DIR" ]]; then
    return
  fi

  mkdir -p "$PROJECT_DIR/systemd"

  local files=(
    organize.py
    review_web.py
    doc_assistant_service.py
    assistant_config.py
    requirements.txt
    install.sh
    uninstall.sh
    LICENSE
    README.md
    category_hints.json
    customer_number_hints.json
  )

  local rel=""
  for rel in "${files[@]}"; do
    if [[ -f "$SOURCE_DIR/$rel" ]]; then
      cp "$SOURCE_DIR/$rel" "$PROJECT_DIR/$rel"
    fi
  done

  if [[ -f "$SOURCE_DIR/systemd/ollama-document-assistant.service" ]]; then
    cp "$SOURCE_DIR/systemd/ollama-document-assistant.service" "$PROJECT_DIR/systemd/ollama-document-assistant.service"
  fi
}

maybe_delete_bootstrap_installer() {
  local script_path="$INSTALL_SCRIPT_PATH"
  if [[ -z "$script_path" || ! -f "$script_path" ]]; then
    return
  fi

  local script_dir
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"

  # Keep installer if it belongs to a repository checkout.
  if [[ -d "$script_dir/.git" ]] || has_project_sources "$script_dir"; then
    return
  fi

  # Never remove the installer inside the active install directory.
  if [[ "$script_path" == "$PROJECT_DIR/install.sh" ]]; then
    return
  fi

  rm -f "$script_path" && echo "Removed transient installer script: $script_path"
}

write_user_service_unit() {
  local unit_path="$HOME/.config/systemd/user/ollama-document-assistant.service"
  cat > "$unit_path" <<EOF
[Unit]
Description=Ollama Document Assistant (dry-run scanner + review web)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/doc_assistant_service.py --config-file $PROJECT_DIR/assistant_config.json
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

set_or_generate_web_password() {
  local password_file="$1"
  local entered=""
  local confirm=""

  # In non-interactive mode, keep existing password or generate a new one.
  if [[ ! -t 0 ]]; then
    if [[ -f "$password_file" ]]; then
      chmod 600 "$password_file"
      echo "Keeping existing password file (non-interactive mode): $password_file"
      return
    fi
    "$VENV_DIR/bin/python" - <<'PY' > "$password_file"
import secrets
print(secrets.token_urlsafe(24))
PY
    chmod 600 "$password_file"
    echo "Generated password file (non-interactive mode): $password_file"
    return
  fi

  while true; do
    read -r -s -p "Web-Login Passwort (leer = automatisch erzeugen): " entered
    echo

    if [[ -z "$entered" ]]; then
      "$VENV_DIR/bin/python" - <<'PY' > "$password_file"
import secrets
print(secrets.token_urlsafe(24))
PY
      chmod 600 "$password_file"
      echo "Generated password file: $password_file"
      return
    fi

    read -r -s -p "Passwort bestaetigen: " confirm
    echo

    if [[ "$entered" != "$confirm" ]]; then
      echo "[WARN] Passwoerter stimmen nicht ueberein, bitte erneut eingeben."
      continue
    fi

    printf '%s\n' "$entered" > "$password_file"
    chmod 600 "$password_file"
    echo "Saved password file: $password_file"
    return
  done
}

ensure_assistant_config() {
  if [[ -f "$PROJECT_DIR/assistant_config.json" ]]; then
    return
  fi
  cat > "$PROJECT_DIR/assistant_config.json" <<'JSON'
{
  "review_web": {
    "host": "127.0.0.1",
    "port": 8449,
    "log_file": "logs/organize_log.jsonl",
    "state_file": "review_state.json",
    "field_aliases_file": "field_aliases.json",
    "auth_password_file": ".review_web_password",
    "session_ttl_seconds": 28800
  },
  "service": {
    "input": "./inbox",
    "output": "./output",
    "model": "qwen2.5:7b-instruct",
    "interval_seconds": 300,
    "host": "127.0.0.1",
    "port": 8449,
    "state_file": "review_state.json",
    "field_aliases_file": "field_aliases.json",
    "auth_password_file": ".review_web_password",
    "session_ttl_seconds": 28800,
    "organize_extra_args": [
      "--ollama-timeout",
      "1800",
      "--ollama-retries",
      "0"
    ]
  }
}
JSON
}

read_models_from_config() {
  "$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
p = Path("assistant_config.json")
if not p.exists():
    print("qwen2.5:7b-instruct")
    raise SystemExit(0)
try:
    cfg = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("qwen2.5:7b-instruct")
    raise SystemExit(0)
service = cfg.get("service", {}) if isinstance(cfg, dict) else {}
model = str(service.get("model", "")).strip() if isinstance(service, dict) else ""
print(model or "qwen2.5:7b-instruct")
PY
}

while [[ $# -gt 0 ]]; do
  arg="$1"
  case "$arg" in
    --help)
      print_usage
      exit 0
      ;;
    --full-setup)
      INSTALL_SYSTEM_DEPS=1
      INSTALL_OLLAMA=1
      PULL_MODELS=1
      ;;
    --install-system-deps)
      INSTALL_SYSTEM_DEPS=1
      ;;
    --install-ollama)
      INSTALL_OLLAMA=1
      ;;
    --pull-models)
      PULL_MODELS=1
      ;;
    --model)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[ERROR] --model requires a value" >&2
        exit 2
      fi
      MODEL_OVERRIDE="$1"
      ;;
    --install-dir)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[ERROR] --install-dir requires a value" >&2
        exit 2
      fi
      PROJECT_DIR="$1"
      ;;
    --repo-url)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[ERROR] --repo-url requires a value" >&2
        exit 2
      fi
      REPO_URL="$1"
      ;;
    --repo-ref)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[ERROR] --repo-ref requires a value" >&2
        exit 2
      fi
      REPO_REF="$1"
      ;;
    --no-systemd)
      INSTALL_SYSTEMD=0
      ;;
    --no-start)
      START_SERVICE=0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
  shift
done

PROJECT_DIR="${PROJECT_DIR/#\~/$HOME}"
if [[ "$PROJECT_DIR" != /* ]]; then
  PROJECT_DIR="$PWD/$PROJECT_DIR"
fi
if [[ "$PROJECT_DIR" == *" "* ]]; then
  echo "[ERROR] Install path must not contain spaces for systemd ExecStart compatibility: $PROJECT_DIR" >&2
  exit 2
fi
mkdir -p "$PROJECT_DIR"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo "[0/7] Preflight"
if ! cmd_exists "$PYTHON_BIN"; then
  echo "[ERROR] Python interpreter not found: $PYTHON_BIN" >&2
  exit 2
fi
echo "Install dir: $PROJECT_DIR"

echo "[prep] Ensuring project checkout"
ensure_project_checkout

echo "[prep] Syncing project files to install dir"
sync_project_files

if [[ "$INSTALL_SYSTEM_DEPS" -eq 1 ]]; then
  if ! cmd_exists apt-get; then
    echo "[ERROR] --install-system-deps requires apt-get (Debian/Ubuntu)." >&2
    exit 2
  fi
fi

if [[ "$INSTALL_SYSTEM_DEPS" -eq 1 ]]; then
  echo "[1/7] Installing system dependencies"
  run_as_root apt-get update
  run_as_root apt-get install -y curl python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-deu poppler-utils
else
  echo "[1/7] Skipping system dependencies (use --install-system-deps or --full-setup)"
fi

if [[ "$INSTALL_OLLAMA" -eq 1 ]]; then
  echo "[2/7] Installing/starting Ollama"
  if ! cmd_exists curl; then
    echo "[ERROR] curl is required to install Ollama" >&2
    exit 2
  fi
  if ! cmd_exists ollama; then
    curl -fsSL https://ollama.com/install.sh | sh
  else
    echo "Ollama already installed: $(command -v ollama)"
  fi

  if cmd_exists systemctl; then
    run_as_root systemctl enable ollama || true
    run_as_root systemctl start ollama || true
  fi
else
  echo "[2/7] Skipping Ollama installation (use --install-ollama or --full-setup)"
fi

echo "[3/7] Preparing Python virtual environment"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "[4/7] Ensuring required folders/files"
mkdir -p "$PROJECT_DIR/inbox"
ensure_assistant_config

echo "[5/7] Ensuring web password file"
set_or_generate_web_password "$PROJECT_DIR/.review_web_password"

if [[ "$PULL_MODELS" -eq 1 ]]; then
  echo "[6/7] Pulling Ollama model(s)"
  if ! cmd_exists ollama; then
    echo "[ERROR] ollama not found. Use --install-ollama or install manually first." >&2
    exit 2
  fi

  MODELS_RAW="$MODEL_OVERRIDE"
  if [[ -z "$MODELS_RAW" ]]; then
    MODELS_RAW="$(cd "$PROJECT_DIR" && read_models_from_config)"
  fi

  if [[ -z "$MODELS_RAW" ]]; then
    MODELS_RAW="qwen2.5:7b-instruct"
  fi

  IFS=',' read -r -a MODELS <<< "$MODELS_RAW"
  for model in "${MODELS[@]}"; do
    model="$(echo "$model" | xargs)"
    [[ -z "$model" ]] && continue
    echo "Pulling model: $model"
    ollama pull "$model"
  done

  echo "Checking Ollama API availability"
  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null; then
    echo "[ERROR] Ollama API not reachable on 127.0.0.1:11434" >&2
    exit 2
  fi
else
  echo "[6/7] Skipping model pull (use --pull-models or --full-setup)"
fi

echo "[7/7] Basic sanity checks"
"$VENV_DIR/bin/python" -m py_compile "$PROJECT_DIR/organize.py" "$PROJECT_DIR/review_web.py" "$PROJECT_DIR/doc_assistant_service.py"

echo "[post] Installing systemd user unit (optional)"
if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
  mkdir -p "$HOME/.config/systemd/user"
  write_user_service_unit
  systemctl --user daemon-reload
  systemctl --user enable ollama-document-assistant.service

  if [[ "$START_SERVICE" -eq 1 ]]; then
    systemctl --user restart ollama-document-assistant.service
  fi
fi

echo "[done] Installation finished"
echo "Project dir: $PROJECT_DIR"
echo "Config file: $PROJECT_DIR/assistant_config.json"
echo "Password file: $PROJECT_DIR/.review_web_password"
echo "Review URL: http://127.0.0.1:8449"

echo "Current web password:"
head -n 1 "$PROJECT_DIR/.review_web_password"

maybe_delete_bootstrap_installer
