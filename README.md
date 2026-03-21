# Valentine v2 — ZeroClaw Multi-Agent AI Assistant

A personal AI assistant operating through Telegram, powered by a multi-agent architecture called **ZeroClaw**. Each agent is a deeply specialized independent process that masters its domain — from code generation to vision analysis to long-term memory.

All LLM inference runs on free-tier APIs (Groq, Cerebras, SambaNova). The system is designed to run on an Oracle Cloud free-tier ARM64 VM (1 OCPU, 6GB RAM).

---

## Architecture

```
  Telegram User
       │
  ┌────▼────┐
  │  Nexus  │  Platform Adapter (Telegram, future: WhatsApp, Slack, Email)
  └────┬────┘
       │
  ┌────▼─────┐
  │ ZeroClaw │  Intent Router (classifies and dispatches)
  └────┬─────┘
       │ Redis Streams
  ┌────▼──────────────────────────────┐
  │          Agent Pool               │
  │  CodeSmith · Oracle · Iris · Echo │
  └──────────────┬────────────────────┘
            ┌────▼────┐
            │ Cortex  │  Memory (Mem0 + Qdrant)
            └─────────┘
```

### Agents

| Agent | Domain | Primary Model |
|-------|--------|---------------|
| **ZeroClaw** | Intent classification & routing | Llama 3 8B via Groq |
| **CodeSmith** | Code generation, debugging, shell execution, GitHub, DevOps | Qwen 32B via Groq |
| **Oracle** | Research, web search, reasoning, conversation | Qwen 32B via Cerebras |
| **Iris** | Image analysis, OCR, image generation, visual Q&A | Qwen-VL via SambaNova |
| **Echo** | Speech-to-text (Whisper), text-to-speech (Edge TTS) | Groq Whisper API |
| **Cortex** | Long-term memory, user profiling, context injection | Mem0 + Qdrant |
| **Nexus** | Telegram adapter, media handling, response formatting | — |

### API Providers

| Provider | Role | Free Tier |
|----------|------|-----------|
| Groq | Primary inference, Whisper STT | ~30 req/min, ~14,400 req/day |
| Cerebras | Secondary inference | ~30 req/min, ~1,000 req/day |
| SambaNova | Multimodal, tertiary | ~10-20 req/min |

Each agent has its own fallback chain. If one provider is rate-limited, requests automatically route to the next.

---

## Capabilities

- **Text & Reasoning** — Deep conversation, research, web search, summarization
- **Code & DevOps** — Code generation, debugging, sandboxed shell execution, file operations, GitHub integration
- **Vision** — Image analysis, OCR, diagram interpretation, image generation (Pollinations.ai)
- **Voice** — Voice note transcription, text-to-speech responses
- **Memory** — Persistent user memory via Mem0 + Qdrant vector database. Valentine remembers your preferences, projects, and context across conversations
- **Multi-agent Chaining** — Complex requests are automatically split across agents (e.g., "analyze this screenshot and fix the bug" → Iris → CodeSmith)

---

## Project Structure

```
valentine/
├── .env                          # API keys (never committed)
├── pyproject.toml                # Dependencies and project config
├── docker-compose.yml            # Qdrant service
├── scripts/
│   ├── setup.sh                  # Full setup script
│   ├── deploy.sh                 # Deploy to Oracle VM
│   └── health_check.sh           # Component health check
├── src/valentine/
│   ├── main.py                   # Entry point — process supervisor
│   ├── config.py                 # Settings and environment variables
│   ├── models.py                 # Shared data models
│   ├── utils.py                  # Logging and shared utilities
│   ├── llm/
│   │   ├── provider.py           # LLM abstraction + fallback chain
│   │   ├── quota.py              # Rate limit tracking
│   │   ├── groq.py               # Groq API client
│   │   ├── cerebras.py           # Cerebras API client
│   │   └── sambanova.py          # SambaNova API client
│   ├── bus/
│   │   └── redis_bus.py          # Redis Streams + pub/sub wrapper
│   ├── orchestrator/
│   │   └── zeroclaw.py           # Intent router
│   ├── agents/
│   │   ├── base.py               # BaseAgent ABC
│   │   ├── codesmith.py          # Code & DevOps agent
│   │   ├── oracle.py             # Research & reasoning agent
│   │   ├── iris.py               # Vision & image agent
│   │   ├── echo.py               # Voice agent
│   │   └── cortex.py             # Memory agent
│   └── nexus/
│       ├── adapter.py            # PlatformAdapter ABC
│       └── telegram.py           # Telegram adapter
├── workspace/                    # Shared agent workspace
└── tests/
```

---

## Requirements

- Python 3.11+
- Redis
- Docker (for Qdrant)
- ffmpeg (for voice processing)

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/Tee-David/valentine.git
cd valentine
cp .env.example .env
# Edit .env with your API keys
```

### 2. Environment variables

```
GROQ_API_KEY=your_groq_key
CEREBRAS_API_KEY=your_cerebras_key
SAMBANOVA_API_KEY=your_sambanova_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
```

Get your keys:
- Groq: https://console.groq.com
- Cerebras: https://cloud.cerebras.ai
- SambaNova: https://cloud.sambanova.ai
- Telegram Bot Token: message @BotFather on Telegram

### 3. Run setup

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This installs Redis, Docker, ffmpeg, starts Qdrant, creates a Python venv, installs dependencies, and sets up swap.

### 4. Start Valentine

```bash
source .venv/bin/activate
python -m valentine.main
```

Or as a systemd service on the Oracle VM:

```bash
sudo cp valentine.service /etc/systemd/system/
sudo systemctl enable valentine
sudo systemctl start valentine
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

---

## Communication Flow

1. User sends a message on Telegram
2. **Nexus** receives it, wraps it as an `IncomingMessage`, writes to Redis Stream
3. **ZeroClaw** reads it, fetches memory context from **Cortex** (<50ms vector lookup)
4. **ZeroClaw** classifies intent and dispatches to the target agent via Redis Stream
5. Target agent processes the request using its specialized LLM + tools
6. Agent writes result back to Redis Stream
7. **ZeroClaw** receives result (or chains to next agent if multi-step)
8. Final response is published to **Nexus** via pub/sub
9. **Nexus** formats and sends the response back on Telegram
10. **Cortex** asynchronously extracts and stores new memories

---

## Deployment (Oracle Cloud Free Tier)

### RAM Budget (~6GB)

| Component | RAM |
|-----------|-----|
| OS + systemd | ~500MB |
| Redis | ~20MB |
| Qdrant (Docker) | ~400MB |
| All agent processes | ~800MB |
| Headroom | ~4GB |

### Systemd Service

```ini
[Unit]
Description=Valentine AI - ZeroClaw Multi-Agent System
After=network.target redis-server.service docker.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/valentine
ExecStart=/home/ubuntu/valentine/.venv/bin/python -m valentine.main
ExecStop=/bin/kill -SIGTERM $MAINPID
TimeoutStopSec=45
Restart=always
RestartSec=5
EnvironmentFile=/home/ubuntu/valentine/.env

[Install]
WantedBy=multi-user.target
```

---

## Health Check

```bash
./scripts/health_check.sh
```

Checks Redis, Qdrant, and Valentine process status.

---

## Future Roadmap

- WhatsApp adapter (Baileys)
- Slack adapter (Bolt SDK)
- Email adapter (IMAP/SMTP)
- MCP server support for CodeSmith
- GitHub Actions integration
- Proactive notifications and scheduled tasks
