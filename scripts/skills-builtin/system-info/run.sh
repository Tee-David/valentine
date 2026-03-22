#!/bin/bash
# DESC: Get system information: CPU, memory, disk usage, uptime

case "${1:-all}" in
    cpu)
        echo "=== CPU Info ==="
        nproc
        cat /proc/loadavg 2>/dev/null || uptime
        ;;
    memory|mem)
        echo "=== Memory ==="
        free -h 2>/dev/null || vm_stat 2>/dev/null
        ;;
    disk)
        echo "=== Disk Usage ==="
        df -h / /tmp 2>/dev/null
        ;;
    uptime)
        echo "=== Uptime ==="
        uptime
        ;;
    all|*)
        echo "=== System Info ==="
        echo "Hostname: $(hostname)"
        echo "OS: $(uname -srm)"
        echo "Uptime: $(uptime -p 2>/dev/null || uptime)"
        echo ""
        echo "=== CPU ==="
        echo "Cores: $(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null)"
        echo "Load: $(cat /proc/loadavg 2>/dev/null | cut -d' ' -f1-3 || uptime | awk -F'load average:' '{print $2}')"
        echo ""
        echo "=== Memory ==="
        free -h 2>/dev/null || vm_stat 2>/dev/null
        echo ""
        echo "=== Disk ==="
        df -h / /tmp 2>/dev/null
        ;;
esac
