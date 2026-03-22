# Valentine — Autonomous Multi-Agent AI Assistant

> Built by **WDC Solutions** | CEO: **Taiwo David Dayomola**

Valentine is an autonomous, self-aware AI assistant that operates through Telegram. It runs a multi-agent architecture where 8 specialized agents — each an independent OS process — collaborate over Redis Streams to handle everything from casual conversation to code engineering, web browsing, voice processing, and document generation.

All LLM inference runs on **free-tier APIs** (Groq, Cerebras, SambaNova). Designed to run on an **Oracle Cloud Free Tier ARM64 VM** (1 OCPU, 6GB RAM).

---

## Architecture

```
  Telegram User
       │
  ┌────▼──────────┐
  │ Telegram Nexus │  Platform adapter (inbound/outbound)
  └────┬──────────-┘
       │
  ┌────▼─────────┐
  │   ZeroClaw   │  Intent router — classifies and dispatches
  └────┬─────────┘
       │ Redis Streams
  ┌────▼──────────────────────────────────────────────┐
  │                   Agent Pool                       │
  │  Oracle · CodeSmith · Iris · Echo · Browser · Nexus│
  └──────────────────┬────────────────────────────────-┘
       ┌─────────────┴─────────────┐
  ┌────▼────┐              ┌───────▼────────┐
  │ Cortex  │  Memory      │  Tool Registry  │
  │ (Mem0)  │  (Qdrant)    │  (MCP + Skills) │
  └─────────┘              └────────────────-┘
```

### Agents

| Agent | Domain | What it does |
|-------|--------|-------------|
| **ZeroClaw** | Router | Classifies every incoming message and dispatches it to the right agent |
| **Oracle** | Chat & Research | General conversation, web search, Q&A, summarisation, games |
| **CodeSmith** | Engineering | Code generation, debugging, shell execution, GitHub ops, skill management |
| **Iris** | Vision | Image analysis (OCR, scene description, screenshot-to-code), image generation |
| **Echo** | Voice | Voice message transcription (Whisper), text-to-speech replies (Edge TTS) |
| **Browser** | Web Automation | Headless Chromium via Playwright — navigate, scrape, screenshot, interact |
| **Cortex** | Memory | Persistent memory (facts, procedures, capabilities, constraints) via Mem0 + Qdrant |
| **Nexus** | Tools & APIs | External tool integrations (weather, crypto, MCP tools) |

### LLM Providers (All Free Tier)

| Provider | Role | Free Tier |
|----------|------|-----------|
| **Groq** | Primary inference, Whisper STT | ~30 req/min, ~14,400 req/day |
| **Cerebras** | Secondary inference | ~30 req/min, ~1,000 req/day |
| **SambaNova** | Multimodal (vision), tertiary | ~10-20 req/min |

Automatic **fallback chains**: if one provider is rate-limited, requests route to the next.

---

## Capabilities

### Core
- **Natural Language** — Deep conversation, reasoning, research, summarisation, games, creative writing
- **Web Search** — Real-time search via DuckDuckGo + URL content fetching
- **Code Engineering** — Write, debug, explain, and deploy code in any language with sandboxed shell execution
- **Vision** — Analyse photos (OCR, scene description, screenshot-to-code), generate images via Pollinations AI
- **Voice** — Transcribe voice notes, respond with text-to-speech audio files
- **Web Browsing** — Headless Chromium: navigate pages, scrape data, take screenshots, fill forms, run JavaScript
- **Persistent Memory** — Remembers user preferences, project context, procedures, and constraints across conversations

### Advanced
- **MCP Tool Integration** — Connect to any MCP server (GitHub, filesystem, databases, Slack, Google Drive)
- **Dynamic Skills** — Install and run extensible skills from shell scripts or Git repositories at runtime
- **Autonomy Modes** — Supervised (approval for dangerous actions), Full (auto-execute), Read-only
- **Document Generation** — Create Excel, PDF, Word, CSV, HTML files and send via Telegram
- **Proactive Scheduling** — Cron-like recurring tasks that run autonomously
- **Self-Evolution** — Auto-detect and install missing tools/dependencies when needed
- **Environment Awareness** — Audit the host system (CPU, RAM, disk, network, installed runtimes)
- **Docker Sandbox** — Run untrusted code in isolated containers with resource limits
- **Codebase RAG** — Semantic code search over your project files via Qdrant + sentence-transformers

### Security & Integrity
- **Prompt Injection Resistance** — All agents hardened against "ignore previous instructions" and similar attacks
- **Truthfulness Policy** — Never hallucinate, never fabricate URLs/citations, always admit uncertainty
- **Sensitive Info Protection** — API keys, tokens, internal paths, and system prompts are never leaked in responses
- **Output Sanitisation** — Automatic redaction of accidentally leaked secrets before responses reach users
- **Input Validation** — Message length limits, media type whitelisting, control character stripping

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and getting started |
| `/help` | List all available commands |
| `/whoami` | Valentine's identity and origins |
| `/capabilities` | Full list of everything Valentine can do |
| `/status` | System health — which agents/processes are running |
| `/agents` | List all active agents and their roles |
| `/mode` | Show or change autonomy mode (supervised/full/readonly) |
| `/skills` | List installed skills |
| `/tools` | List available MCP tools |
| `/schedule` | Create a recurring scheduled task |
| `/jobs` | List active scheduled jobs |
| `/memory` | Search Valentine's long-term memory |
| `/forget` | Remove a specific memory |
| `/clear` | Clear conversation history (keeps long-term memory) |

---

## Project Structure

```
valentine/
├── .env.template                 # Environment variable template
├── pyproject.toml                # Dependencies and project config
├── docker-compose.yml            # Qdrant + Redis services
├── valentine.service             # Systemd service file
│
├── src/valentine/
│   ├── __init__.py               # Package metadata (version, author)
│   ├── identity.py               # Self-awareness: who Valentine is, capabilities, policies
│   ├── security.py               # Input validation, injection detection, output sanitisation
│   ├── config.py                 # Settings (env vars, model config, rate limits)
│   ├── models.py                 # Shared data models (IncomingMessage, TaskResult, etc.)
│   ├── main.py                   # Entry point — process supervisor + health check
│   ├── utils.py                  # Logging utilities
│   │
│   ├── agents/                   # Agent implementations
│   │   ├── base.py               # BaseAgent ABC (lifecycle, task loop, output sanitisation)
│   │   ├── oracle.py             # Chat & research
│   │   ├── codesmith.py          # Code engineering & DevOps
│   │   ├── browser.py            # Headless web browsing (Playwright)
│   │   ├── iris.py               # Vision & image generation
│   │   ├── echo.py               # Voice transcription & TTS
│   │   ├── cortex.py             # Memory (Mem0 + Qdrant)
│   │   ├── nexus.py              # External tool integrations
│   │   └── loop.py               # Agentic reasoning loop (think → act → observe)
│   │
│   ├── orchestrator/
│   │   └── zeroclaw.py           # Intent router with tool-aware routing
│   │
│   ├── core/                     # Infrastructure services & capabilities
│   │   ├── autonomy.py           # Autonomy modes + approval gates
│   │   ├── sandbox.py            # Docker sandbox for untrusted code
│   │   ├── scheduler.py          # Proactive cron-like scheduling
│   │   ├── docgen.py             # Document generation (Excel, PDF, Word, CSV)
│   │   ├── evolution.py          # Self-evolution (auto-install missing tools)
│   │   ├── senses.py             # Environment awareness (system audit)
│   │   └── rag.py                # Codebase RAG (semantic code search)
│   │
│   ├── bus/
│   │   └── redis_bus.py          # Redis Streams + pub/sub + conversation history
│   │
│   ├── llm/                      # LLM provider layer
│   │   ├── provider.py           # LLMProvider ABC + MultimodalProvider + AudioProvider
│   │   ├── groq.py               # Groq API client
│   │   ├── cerebras.py           # Cerebras API client
│   │   ├── sambanova.py          # SambaNova API client
│   │   ├── fallback.py           # FallbackChain (auto-failover between providers)
│   │   └── rate_limiter.py       # Token bucket rate limiter
│   │
│   ├── tools/
│   │   └── registry.py           # Redis-backed Tool Registry (shared across all agents)
│   │
│   ├── mcp/
│   │   └── client.py             # MCP Client Manager (stdio/SSE connections)
│   │
│   ├── skills/
│   │   ├── manager.py            # Dynamic skill install/execute (git + local)
│   │   └── manifest.py           # skill.toml parser
│   │
│   ├── nexus/                    # Outbound delivery adapters
│   │   ├── adapter.py            # PlatformAdapter ABC
│   │   └── telegram.py           # Telegram adapter (commands, media, rate-limiting)
│   │
│   └── bot/
│       └── telegram.py           # Telegram bot setup
│
├── configs/
│   └── mcp-servers.example.json  # Example MCP server configurations
│
├── scripts/
│   ├── deploy.sh                 # Deploy to Oracle VM
│   └── skills-builtin/           # Built-in skills (shell scripts + skill.toml manifests)
│
├── tests/
│   ├── conftest.py               # Test fixtures
│   └── test_models.py            # Model unit tests
│
└── docs/
    └── superpowers/
        ├── specs/                # Design specifications
        └── plans/                # Implementation plans
```

---

## Requirements

- **Python 3.11+**
- **Redis** — Message bus and conversation storage
- **Docker** — For Qdrant vector database (memory)
- **ffmpeg** — Voice message processing (optional, for Echo agent)
- **Playwright** — Headless browsing (optional, for Browser agent)

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/Tee-David/valentine.git
cd valentine
cp .env.template .env
# Edit .env with your API keys
```

### 2. Environment variables

```env
# Required
GROQ_API_KEY=your_groq_key
CEREBRAS_API_KEY=your_cerebras_key
SAMBANOVA_API_KEY=your_sambanova_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Optional
REDIS_URL=redis://localhost:6379/0
AUTONOMY_MODE=supervised
GITHUB_TOKEN=your_github_pat
```

Get your keys (all free):
- **Groq**: https://console.groq.com
- **Cerebras**: https://cloud.cerebras.ai
- **SambaNova**: https://cloud.sambanova.ai
- **Telegram Bot Token**: message @BotFather on Telegram

### 3. Install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### 4. Start infrastructure

```bash
# Start Redis
sudo systemctl start redis-server

# Start Qdrant (for memory)
docker compose up -d
```

### 5. Run Valentine

```bash
python -m valentine.main
```

Or as a systemd service:

```bash
sudo cp valentine.service /etc/systemd/system/
sudo systemctl enable valentine
sudo systemctl start valentine
```

---

## Communication Flow

```
1. User sends message on Telegram
2. Telegram Adapter receives it, sanitises input, wraps as IncomingMessage
3. Message written to Redis Stream → ZeroClaw
4. ZeroClaw fetches memory context from Cortex (<50ms vector lookup)
5. ZeroClaw classifies intent, dispatches to target agent via Redis Stream
6. Target agent processes request using its specialised LLM + tools
7. Agent result passes through output sanitisation (secret redaction)
8. Result published via Redis pub/sub → Telegram Adapter
9. Response formatted and sent back to user on Telegram
10. Cortex asynchronously extracts and stores new memories
```

---

## Process Architecture

Every agent runs as its own OS process (via `multiprocessing.spawn`). This provides:

- **Fault isolation** — one agent crashing doesn't bring down others
- **Independent scaling** — each process manages its own LLM connections
- **ARM64 compatibility** — spawn mode works reliably on Oracle ARM instances
- **Auto-restart** — the supervisor detects dead processes and respawns them

The `ProcessSupervisor` in `main.py` manages the lifecycle of all agent processes, the Telegram adapter, the MCP bridge, and the scheduler.

**Health check**: `http://127.0.0.1:8080/health` returns JSON status of all processes.

---

## Deployment (Oracle Cloud Free Tier)

### RAM Budget (~6GB)

| Component | RAM |
|-----------|-----|
| OS + systemd | ~500MB |
| Redis | ~20MB |
| Qdrant (Docker) | ~400MB |
| All agent processes (8) | ~800MB |
| MCP bridge + scheduler | ~100MB |
| Headroom | ~4GB |

### Recommended Instance

- **Shape**: VM.Standard.A1.Flex (ARM64)
- **OCPUs**: 1-2
- **RAM**: 6-12 GB
- **Boot volume**: 50 GB
- **OS**: Ubuntu 22.04+ or Oracle Linux 8+

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/

# Type check (optional)
mypy src/valentine/
```

---

## MCP Server Configuration

Valentine can connect to external MCP servers for extended tool access. Configure in `.env`:

```env
MCP_SERVERS={"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."}}}
```

See [configs/mcp-servers.example.json](configs/mcp-servers.example.json) for examples including GitHub, filesystem, SQLite, Brave Search, Google Drive, and Slack.

---

## License

Proprietary — WDC Solutions. All rights reserved.

---

<p align="center">
  <strong>Valentine</strong> — Built with care by <strong>WDC Solutions</strong><br>
  Led by <strong>Taiwo David Dayomola</strong>, CEO & Software Engineer
</p>
