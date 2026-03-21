#!/usr/bin/env bash
set -euo pipefail

# Valentine v2 — Deployment Script
# Usage: ./scripts/deploy.sh [--install | --update | --status]

APP_DIR="/opt/valentine"
SERVICE_NAME="valentine"
VENV_DIR="$APP_DIR/venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; }

check_deps() {
    for cmd in python3 docker redis-cli ffmpeg; do
        if ! command -v "$cmd" &>/dev/null; then
            error "Missing dependency: $cmd"
            exit 1
        fi
    done
    info "All system dependencies present."
}

start_services() {
    info "Starting Docker services (Redis + Qdrant)..."
    cd "$APP_DIR"
    docker compose up -d
    sleep 2

    # Verify Redis is reachable
    if redis-cli -u "${REDIS_URL:-redis://localhost:6379}" ping | grep -q PONG; then
        info "Redis is healthy."
    else
        error "Redis is not responding."
        exit 1
    fi
    info "Docker services running."
}

install_app() {
    info "Installing Valentine v2..."
    check_deps

    # Create venv if missing
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        info "Virtual environment created."
    fi

    # Install package
    $PIP install --upgrade pip
    $PIP install -e "$APP_DIR"
    info "Python dependencies installed."

    # Ensure workspace directory exists
    mkdir -p /tmp/valentine/workspace

    # Start backing services
    start_services

    # Install systemd unit
    if [ -f "$APP_DIR/valentine.service" ]; then
        sudo cp "$APP_DIR/valentine.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable "$SERVICE_NAME"
        info "Systemd service installed and enabled."
    fi

    info "Installation complete. Run: sudo systemctl start $SERVICE_NAME"
}

update_app() {
    info "Updating Valentine v2..."
    cd "$APP_DIR"
    git pull --ff-only
    $PIP install -e .
    sudo systemctl restart "$SERVICE_NAME"
    info "Update complete."
}

show_status() {
    echo "=== Systemd Service ==="
    sudo systemctl status "$SERVICE_NAME" --no-pager || true
    echo ""
    echo "=== Docker Services ==="
    cd "$APP_DIR" && docker compose ps
    echo ""
    echo "=== Health Check ==="
    curl -s http://127.0.0.1:8080/health 2>/dev/null | python3 -m json.tool || echo "(health endpoint not responding)"
}

case "${1:-}" in
    --install) install_app ;;
    --update)  update_app ;;
    --status)  show_status ;;
    *)
        echo "Usage: $0 [--install | --update | --status]"
        exit 1
        ;;
esac
