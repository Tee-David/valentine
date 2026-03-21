#!/bin/bash
# DESC: Server monitoring — CPU, RAM, disk, processes, network
# CATEGORY: server
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: server-monitor.sh <command>

set -euo pipefail

usage() {
    echo "Valentine Server Monitor"
    echo ""
    echo "Commands:"
    echo "  overview     Full system overview"
    echo "  cpu          CPU usage and load"
    echo "  memory       RAM and swap usage"
    echo "  disk         Disk usage"
    echo "  network      Network connections and interfaces"
    echo "  processes    Top processes by CPU/memory"
    echo "  uptime       System uptime"
    echo "  services     List running systemd services"
    echo "  ports        Open ports"
    echo "  docker       Docker container status"
    echo "  valentine    Valentine-specific health check"
}

cmd_overview() {
    echo "=== System Overview ==="
    echo "Hostname: $(hostname)"
    echo "Kernel:   $(uname -r)"
    echo "Arch:     $(uname -m)"
    echo "Uptime:   $(uptime -p 2>/dev/null || uptime)"
    echo ""
    cmd_cpu
    echo ""
    cmd_memory
    echo ""
    cmd_disk
}

cmd_cpu() {
    echo "=== CPU ==="
    echo "Cores: $(nproc)"
    echo "Load:  $(cat /proc/loadavg | awk '{print $1, $2, $3}')"
    echo ""
    echo "Top CPU consumers:"
    ps aux --sort=-%cpu | head -6 | awk '{printf "  %-8s %5s%% %s\n", $1, $3, $11}'
}

cmd_memory() {
    echo "=== Memory ==="
    free -h | head -3
}

cmd_disk() {
    echo "=== Disk ==="
    df -h --type=ext4 --type=xfs --type=btrfs 2>/dev/null || df -h | grep -v tmpfs | grep -v devtmpfs
}

cmd_network() {
    echo "=== Network Interfaces ==="
    ip -brief addr 2>/dev/null || ifconfig 2>/dev/null | grep -E "^[a-z]|inet "
    echo ""
    echo "=== Active Connections ==="
    ss -tuln 2>/dev/null | head -20 || netstat -tuln 2>/dev/null | head -20
}

cmd_processes() {
    echo "=== Top Processes (by CPU) ==="
    ps aux --sort=-%cpu | head -11
    echo ""
    echo "=== Top Processes (by Memory) ==="
    ps aux --sort=-%mem | head -11
}

cmd_services() {
    echo "=== Active Services ==="
    systemctl list-units --type=service --state=running --no-pager 2>/dev/null | head -30 || echo "systemctl not available"
}

cmd_ports() {
    echo "=== Open Ports ==="
    ss -tuln 2>/dev/null || netstat -tuln 2>/dev/null
}

cmd_docker() {
    echo "=== Docker Containers ==="
    if command -v docker &>/dev/null; then
        docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not running"
    else
        echo "Docker not installed"
    fi
}

cmd_valentine() {
    echo "=== Valentine Health ==="

    # Check systemd service
    if systemctl is-active valentine.service &>/dev/null; then
        echo "Service:  RUNNING"
    else
        echo "Service:  STOPPED"
    fi

    # Check Redis
    if redis-cli ping &>/dev/null; then
        echo "Redis:    UP"
    else
        echo "Redis:    DOWN"
    fi

    # Check health endpoint
    local health
    health=$(curl -s http://127.0.0.1:8080/health 2>/dev/null || echo '{"status":"unreachable"}')
    echo "Health:   $health"

    # Check Valentine processes
    echo ""
    echo "Valentine Processes:"
    ps aux | grep "[v]alentine" | awk '{printf "  PID %-8s CPU %-5s MEM %-5s %s\n", $2, $3, $4, $11}'
}

case "${1:-}" in
    overview)   cmd_overview ;;
    cpu)        cmd_cpu ;;
    memory|mem) cmd_memory ;;
    disk)       cmd_disk ;;
    network|net) cmd_network ;;
    processes|ps) cmd_processes ;;
    uptime)     uptime -p 2>/dev/null || uptime ;;
    services)   cmd_services ;;
    ports)      cmd_ports ;;
    docker)     cmd_docker ;;
    valentine)  cmd_valentine ;;
    -h|--help)  usage ;;
    *)          usage ;;
esac
