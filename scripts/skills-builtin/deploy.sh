#!/bin/bash
# DESC: Deployment management — Valentine service control, updates, rollback
# CATEGORY: deploy
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: deploy.sh <command>

set -euo pipefail

VALENTINE_DIR="${VALENTINE_DIR:-/opt/valentine}"
SERVICE_NAME="valentine.service"

usage() {
    echo "Valentine Deploy Skill"
    echo ""
    echo "Commands:"
    echo "  status       Show service status"
    echo "  restart      Restart Valentine service"
    echo "  stop         Stop Valentine service"
    echo "  start        Start Valentine service"
    echo "  logs [n]     Show last N lines of logs (default: 50)"
    echo "  update       Git pull and restart"
    echo "  rollback     Rollback to previous commit"
    echo "  version      Show current version/commit"
}

cmd_status() {
    systemctl status "$SERVICE_NAME" --no-pager 2>/dev/null || echo "Service not found"
}

cmd_restart() {
    echo "Restarting Valentine..."
    sudo systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl is-active "$SERVICE_NAME" && echo "Valentine restarted successfully" || echo "WARNING: Service may not have started"
}

cmd_stop() {
    echo "Stopping Valentine..."
    sudo systemctl stop "$SERVICE_NAME"
    echo "Valentine stopped."
}

cmd_start() {
    echo "Starting Valentine..."
    sudo systemctl start "$SERVICE_NAME"
    sleep 2
    systemctl is-active "$SERVICE_NAME" && echo "Valentine started successfully" || echo "WARNING: Service may not have started"
}

cmd_logs() {
    local n="${1:-50}"
    journalctl -u "$SERVICE_NAME" --no-pager -n "$n"
}

cmd_update() {
    echo "Updating Valentine..."
    cd "$VALENTINE_DIR"

    # Save current commit for rollback
    git rev-parse HEAD > .last_good_commit

    git pull origin main 2>&1
    echo ""

    # Reinstall deps if pyproject.toml changed
    if git diff HEAD~1 --name-only | grep -q pyproject.toml; then
        echo "Dependencies changed, reinstalling..."
        pip install -e ".[dev]" 2>&1
    fi

    cmd_restart
    echo "Update complete."
}

cmd_rollback() {
    cd "$VALENTINE_DIR"
    if [ -f .last_good_commit ]; then
        local target
        target=$(cat .last_good_commit)
        echo "Rolling back to commit: $target"
        git checkout "$target"
        cmd_restart
        echo "Rollback complete."
    else
        echo "No rollback point found. Rolling back one commit..."
        git reset --hard HEAD~1
        cmd_restart
    fi
}

cmd_version() {
    cd "$VALENTINE_DIR" 2>/dev/null || cd /opt/valentine 2>/dev/null || { echo "Valentine dir not found"; return 1; }
    echo "Commit: $(git rev-parse --short HEAD)"
    echo "Branch: $(git branch --show-current)"
    echo "Date:   $(git log -1 --format='%ci')"
    echo "Msg:    $(git log -1 --format='%s')"
}

case "${1:-}" in
    status)     cmd_status ;;
    restart)    cmd_restart ;;
    stop)       cmd_stop ;;
    start)      cmd_start ;;
    logs)       shift; cmd_logs "$@" ;;
    update)     cmd_update ;;
    rollback)   cmd_rollback ;;
    version)    cmd_version ;;
    -h|--help)  usage ;;
    *)          usage ;;
esac
