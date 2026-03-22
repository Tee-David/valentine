#!/bin/bash
# scripts/setup-vm.sh
# One-shot setup for Valentine on Oracle Cloud Free Tier ARM64 VM.
# Run as: sudo bash scripts/setup-vm.sh

set -euo pipefail

echo "=== Valentine VM Setup ==="
echo "Platform: $(uname -m)"

# -------------------------------------------------------
# 1. System packages
# -------------------------------------------------------
echo ""
echo "--- Installing system packages ---"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    redis-server \
    ffmpeg \
    curl \
    git \
    jq

# Start Redis
systemctl enable redis-server
systemctl start redis-server
echo "Redis: $(redis-cli ping)"

# -------------------------------------------------------
# 2. Node.js (for MCP servers via npx)
# -------------------------------------------------------
echo ""
echo "--- Installing Node.js ---"
if command -v node &>/dev/null; then
    echo "Node.js already installed: $(node --version)"
else
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq nodejs
    echo "Node.js installed: $(node --version)"
fi
echo "npm: $(npm --version)"

# -------------------------------------------------------
# 3. Cloudflared (for preview tunnels)
# -------------------------------------------------------
echo ""
echo "--- Installing cloudflared ---"
if command -v cloudflared &>/dev/null; then
    echo "cloudflared already installed: $(cloudflared --version)"
else
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
        BINARY="cloudflared-linux-arm64"
    else
        BINARY="cloudflared-linux-amd64"
    fi
    curl -L "https://github.com/cloudflare/cloudflared/releases/latest/download/${BINARY}" \
        -o /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
    echo "cloudflared installed: $(cloudflared --version)"
fi

# -------------------------------------------------------
# 4. Chromium (for Browser agent on ARM64)
# -------------------------------------------------------
echo ""
echo "--- Installing Chromium ---"
if command -v chromium-browser &>/dev/null || command -v chromium &>/dev/null; then
    echo "Chromium already installed"
else
    apt-get install -y -qq chromium-browser || apt-get install -y -qq chromium || {
        echo "WARNING: Could not install Chromium. Browser agent will use HTTP fallback."
    }
fi

# -------------------------------------------------------
# 5. Docker + Qdrant (optional, for memory)
# -------------------------------------------------------
echo ""
echo "--- Docker + Qdrant (for persistent memory) ---"
if command -v docker &>/dev/null; then
    echo "Docker already installed: $(docker --version)"
    if docker ps --filter "name=qdrant" --format '{{.Names}}' | grep -q qdrant; then
        echo "Qdrant already running"
    else
        echo "Starting Qdrant..."
        docker run -d --name qdrant \
            -p 6333:6333 -p 6334:6334 \
            --restart unless-stopped \
            -v qdrant_data:/qdrant/storage \
            qdrant/qdrant:latest 2>/dev/null || echo "Qdrant may already exist. Try: docker start qdrant"
    fi
else
    echo "Docker not installed. Memory (Cortex) will degrade gracefully."
    echo "Install Docker for persistent memory: https://docs.docker.com/engine/install/"
fi

# -------------------------------------------------------
# 6. Python dependencies
# -------------------------------------------------------
echo ""
echo "--- Python setup ---"
VALENTINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VALENTINE_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Created virtual environment"
fi

source venv/bin/activate
pip install -q -e ".[dev]"
echo "Python dependencies installed"

# -------------------------------------------------------
# 7. Workspace directory
# -------------------------------------------------------
mkdir -p /opt/valentine/workspace
mkdir -p /opt/valentine/skills
echo "Workspace dirs created"

# -------------------------------------------------------
# Summary
# -------------------------------------------------------
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Installed:"
command -v python3    && echo "  Python:      $(python3 --version)"
command -v node       && echo "  Node.js:     $(node --version)"
command -v redis-cli  && echo "  Redis:       $(redis-cli ping)"
command -v ffmpeg     && echo "  ffmpeg:      $(ffmpeg -version 2>&1 | head -1)"
command -v cloudflared && echo "  cloudflared: $(cloudflared --version 2>&1 | head -1)"
(command -v chromium-browser || command -v chromium) &>/dev/null && echo "  Chromium:    installed"
command -v docker     && echo "  Docker:      $(docker --version)"
echo ""
echo "Next steps:"
echo "  1. cp .env.template .env && nano .env   # Add your API keys"
echo "  2. source venv/bin/activate"
echo "  3. python -m valentine.main             # Start Valentine"
echo ""
