# Valentine v2: ZeroClaw Multi-Agent Architecture

**Date:** 2026-03-21
**Status:** Approved
**Approach:** Microservice Sub-Agents with Redis message bus

---

## 1. Overview

Valentine is being rebuilt from a single-file Telegram bot into a multi-agent system called **ZeroClaw**. All LLM inference is offloaded to free external APIs (Groq, Cerebras, SambaNova). The Oracle Cloud free-tier VM (1 OCPU, 6GB RAM, ARM64) runs only the orchestrator, sub-agents, Redis, and a lightweight vector DB.

### Core Principles
- Each sub-agent is an independent Python async process, deeply specialized in its domain
- ZeroClaw is a lightweight intent router — it does not do heavy reasoning
- All agents communicate via Redis Streams (reliable) and pub/sub (fire-and-forget where appropriate)
- Platform adapters (Telegram first, WhatsApp/Slack/email later) are pluggable
- Memory (Mem0 + Qdrant) is a shared service all agents can query
- Process spawning uses `multiprocessing` with `spawn` start method (not fork) to avoid asyncio + Redis connection pool issues on ARM64

### API Providers (Free Tier)
| Provider | Models Available | Primary Use |
|----------|-----------------|-------------|
| Groq | Qwen 32B, Llama 3 70B/8B, Whisper | Primary inference, STT |
| Cerebras | Qwen 32B, Llama 3 70B/8B | Secondary/fallback |
| SambaNova | Qwen 32B, Qwen-VL, Llama | Multimodal, tertiary fallback |

### RAM Budget (6GB) — Realistic Estimates
| Component | Estimated RAM |
|-----------|--------------|
| Ubuntu OS + systemd | ~500MB |
| Redis (via apt, no Docker) | ~20MB |
| Qdrant (Docker, ARM64 image) | ~350-500MB |
| Docker runtime overhead | ~50MB |
| ZeroClaw (Python process) | ~80MB |
| Nexus (Telegram adapter) | ~60MB |
| CodeSmith agent | ~80MB |
| Oracle agent | ~80MB |
| Iris agent | ~80MB |
| Echo agent (Whisper tiny + ffmpeg buffers) | ~300MB |
| Cortex (Mem0 + sentence-transformer embeddings) | ~400MB |
| **Total worst-case** | **~2,000-2,300MB** |
| **Headroom** | **~3.7-4.0GB free** |

**Notes:**
- Python process overhead is estimated at 60-80MB per process (asyncio + httpx + redis client at import time, before workload)
- Echo's Whisper tiny is ~75MB model weights but ~300MB resident with ffmpeg audio decode buffers
- Cortex includes a local sentence-transformer (~200-300MB) for embeddings — this is the heaviest non-Qdrant component
- **Recommendation:** Add 1-2GB swap as safety net for transient spikes (concurrent agent processing, Qdrant indexing)

---

## 2. System Architecture

```
                    ┌─────────────────────────┐
                    │      Telegram User       │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │    Nexus (Telegram)      │
                    │    Platform Adapter      │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │       ZeroClaw           │
                    │    Intent Router         │
                    │  (Llama 3 8B via Groq)   │
                    └────────────┬────────────┘
                                 │ Redis
                    ┌────────────▼────────────┐
              ┌─────┤      Redis Bus          ├─────┐
              │     │  Streams + Pub/Sub      │     │
              │     └─────────────────────────┘     │
     ┌────────▼──┐  ┌──────▼──┐  ┌──▼─────┐  ┌──▼────┐
     │ CodeSmith  │  │ Oracle  │  │  Iris  │  │ Echo  │
     │ Code/DevOps│  │Research │  │ Vision │  │ Voice │
     └────────────┘  └─────────┘  └────────┘  └───────┘
              │           │           │           │
              └───────────┴─────┬─────┴───────────┘
                          ┌─────▼─────┐
                          │  Cortex   │
                          │  Memory   │
                          │ Mem0+Qdrant│
                          └───────────┘
```

---

## 3. Component Specifications

### 3.1 ZeroClaw — Intent Router

**Purpose:** Parse user intent and route to the correct sub-agent. Does NOT do heavy reasoning.

**Model:** Llama 3 8B via Groq (fastest, cheapest — only classifying intent).

**Input:** Raw user message + message type (text, photo, voice, document)
**Output:** Structured JSON routing decision:
```json
{
    "intent": "code_generation",
    "agent": "codesmith",
    "priority": "normal",
    "context_needed": true,
    "chain": null,
    "params": {
        "language": "python",
        "task": "write a fastapi endpoint"
    }
}
```

**Routing Table:**
| Signal | Agent | Examples |
|--------|-------|----------|
| Code, debug, GitHub, deploy, shell, file ops | CodeSmith | "write a script", "check server logs" |
| Questions, research, reasoning, web search | Oracle | "what's happening in tech", "explain X" |
| Photo received, image analysis, image gen | Iris | sent a photo, "generate an image of..." |
| Voice note received | Echo | any audio message |
| "Remember...", "what did I say about..." | Cortex | explicit memory ops |
| Casual chat, greetings | Oracle | "hey valentine" |

**Multi-agent chaining:** Some requests need multiple agents in sequence. ZeroClaw detects this and creates a pipeline:
- "Look at this screenshot and fix the bug" -> Iris -> CodeSmith
- "Search for X and write code based on it" -> Oracle -> CodeSmith
- "Remember what I said about the project and summarize" -> Cortex -> Oracle

### 3.2 CodeSmith — Code & DevOps Agent

**Domain:** Code generation, debugging, refactoring, shell execution, file I/O, GitHub, package management, deployment, server admin.

**Model:** Qwen 32B via Groq (primary). Cerebras Qwen 32B (fallback). SambaNova Qwen 32B (tertiary).

**Capabilities:**
- Code generation in any language with project context awareness
- Sandboxed shell execution (subprocess with timeout + output capture)
- File read/write within allowed directories
- GitHub integration via API: clone, commit, push, PRs, issues
- npm/pip/apt package management
- Docker, docker-compose, systemd service management
- Log tailing, error parsing, diagnostic suggestions
- MCP client — connects to external MCP tool servers

**Security:** Shell execution sandboxed to allowed directories. Dangerous commands (rm -rf /, etc.) blocked by allowlist/denylist pattern.

### 3.3 Oracle — Research & Reasoning Agent

**Domain:** General knowledge, web search, deep reasoning, analysis, summarization, conversation.

**Model:** Qwen 32B via Cerebras (primary — spreads load away from Groq). Groq Llama 3 70B (fallback). SambaNova (tertiary).

**Capabilities:**
- Web search via DuckDuckGo API (free, no key needed)
- URL fetching + readability content extraction
- Multi-step reasoning chains (breaks complex queries into sub-queries)
- Long document summarization
- All casual conversation and general chat
- Chainable — researches context for other agents

### 3.4 Iris — Vision & Image Agent

**Domain:** Image analysis, OCR, image generation, document understanding.

**Model:** Qwen-VL via SambaNova (primary — only provider with multimodal). Groq multimodal (fallback, limited model availability).

**Known limitation:** Vision/multimodal has weaker fallback coverage than text-only agents. If SambaNova is down, Iris degrades to text-only description requests via Groq/Cerebras. Image generation is independent (uses Pollinations.ai API) and unaffected.

**Capabilities:**
- Image analysis and visual Q&A via multimodal models
- OCR — text extraction from screenshots, photos, documents
- Image generation via free APIs (Pollinations.ai / Stable Diffusion endpoints)
- Diagram and chart interpretation
- Screenshot-to-code pipeline (analyzes UI -> passes description to CodeSmith)
- PDF and document analysis

### 3.5 Echo — Voice Agent

**Domain:** Speech-to-text, text-to-speech, voice note processing.

**Model:** Groq Whisper API (primary, high quality). Whisper tiny locally (fallback if Groq is down, ~300MB resident with ffmpeg buffers).

**Capabilities:**
- STT: Groq Whisper API (primary) + Whisper tiny local (fallback)
- TTS: Edge TTS (Microsoft free) or Piper (local)
- Voice note transcription -> routes transcribed text back to ZeroClaw
- Audio format conversion via ffmpeg (OGG from Telegram -> WAV)
- Multilingual voice support

### 3.6 Cortex — Memory Agent

**Domain:** Long-term memory, user profiling, conversation context, knowledge retrieval.

**Model:** Qwen 32B via Cerebras for memory summarization. Local sentence-transformer (all-MiniLM-L6-v2, ~200-300MB) for embeddings.

**Two operational modes:**

1. **Sync context fetch (called on every request, <50ms):** ZeroClaw always calls Cortex before routing. Cortex does a fast vector similarity lookup in Qdrant, returns top-k relevant memories. This is a lightweight read — no LLM call, just embedding + search.

2. **Async memory extraction (runs after response delivery):** After the user gets their response, Cortex analyzes the conversation turn and extracts/stores any new facts, preferences, or context. This uses an LLM call but is non-blocking — the user doesn't wait for it.

**Capabilities:**
- Mem0 integration — stores/retrieves memories per user
- Qdrant (local) as vector database for semantic search
- Automatic memory extraction after conversations (async mode)
- Context injection before agent processing (sync mode)
- User profiling: preferences, communication style, ongoing projects
- Sliding window + semantic retrieval for conversation history

### 3.7 Nexus — Platform Adapter

**Domain:** Messaging platform abstraction.

**Interface:**
```python
class PlatformAdapter(ABC):
    async def listen(self) -> AsyncIterator[IncomingMessage]
    async def send_text(self, chat_id: str, text: str)
    async def send_image(self, chat_id: str, image: bytes)
    async def send_voice(self, chat_id: str, audio: bytes)
    async def send_document(self, chat_id: str, doc: bytes, filename: str)
```

**Telegram adapter (primary):**
- python-telegram-bot library
- Message queue to buffer incoming messages
- Media download handling (photos, voice, documents)
- Markdown response formatting
- Rate limiting

**Future adapters (same interface, zero agent changes):**
- WhatsApp (Baileys / whatsapp-web.js bridge)
- Slack (Bolt SDK)
- Email (IMAP/SMTP)

---

## 4. Communication & Data Flow

### 4.1 Redis Channel Specification

**Redis Streams (reliable, persistent, acknowledged):**
- `stream:zeroclaw.route` — Nexus writes incoming messages, ZeroClaw reads (survives ZeroClaw restart)
- `stream:agent.{name}.task` — ZeroClaw writes tasks, agents read (survives agent restart)
- `stream:agent.{name}.result` — Agents write results, ZeroClaw reads

**Redis Pub/Sub (fire-and-forget, low-latency):**
- `pubsub:nexus.respond` — ZeroClaw publishes final responses, Nexus subscribes (Nexus is always up; lost messages can be re-requested)
- `pubsub:heartbeat` — Agents publish periodic heartbeats for health monitoring

**Shared workspace:** `/home/ubuntu/valentine/workspace/` — agents read/write files here

### 4.2 Request Lifecycle
1. User sends message on Telegram
2. Nexus receives message, wraps as `IncomingMessage` dataclass
3. Nexus writes to `stream:zeroclaw.route`
4. ZeroClaw reads, calls Cortex sync context fetch (<50ms vector lookup)
5. ZeroClaw classifies intent (Llama 3 8B via Groq)
6. ZeroClaw writes task + memory context to `stream:agent.{target}.task`
7. Target agent processes, writes result to `stream:agent.{target}.result`
8. ZeroClaw reads result (or chains to next agent if multi-step)
9. ZeroClaw publishes final response to `pubsub:nexus.respond`
10. Nexus formats and sends response to user on Telegram
11. Cortex async memory extraction fires (non-blocking, user already has response)

### 4.3 Per-Agent API Fallback Chains

Each agent has its own fallback chain with specific model-provider pairs:

| Agent | Primary | Secondary | Tertiary |
|-------|---------|-----------|----------|
| ZeroClaw | Groq / Llama 3 8B | Cerebras / Llama 3 8B | SambaNova / Llama 3 8B |
| CodeSmith | Groq / Qwen 32B | Cerebras / Qwen 32B | SambaNova / Qwen 32B |
| Oracle | Cerebras / Qwen 32B | Groq / Llama 3 70B | SambaNova / Qwen 32B |
| Iris (vision) | SambaNova / Qwen-VL | Groq multimodal (limited) | *degrades to text-only* |
| Iris (image gen) | Pollinations.ai | — | — |
| Echo (STT) | Groq / Whisper | Local Whisper tiny | — |
| Cortex (summarize) | Cerebras / Qwen 32B | Groq / Qwen 32B | SambaNova / Qwen 32B |

**Load spreading:** Oracle defaults to Cerebras (not Groq) to avoid competing with CodeSmith and ZeroClaw for Groq rate limits.

If all providers fail for a given request, the task is queued in Redis with exponential backoff (1s, 2s, 4s, max 30s).

### 4.4 Rate Limit & Quota Management

A `QuotaTracker` module in `llm/` maintains sliding-window counters per provider:
- Tracks requests-per-minute and requests-per-day per provider
- Enables **preemptive fallback**: if a provider is at 80% of its rate limit, proactively route to the next provider instead of waiting for a 429
- Persists daily counters in Redis (survives process restarts)
- Approximate free-tier limits tracked:
  - Groq: ~30 req/min, ~14,400 req/day (large models)
  - Cerebras: ~30 req/min, ~1,000 req/day
  - SambaNova: ~10-20 req/min

---

## 5. Project Structure

```
valentine/
├── .env                        # API keys (GROQ_API_KEY, CEREBRAS_API_KEY, SAMBANOVA_API_KEY, TELEGRAM_BOT_TOKEN)
├── pyproject.toml              # Project config + dependencies
├── docker-compose.yml          # Qdrant service (Redis installed via apt)
├── scripts/
│   ├── setup.sh                # Full setup: install deps, start services, verify health
│   ├── deploy.sh               # Deploy to Oracle VM (rsync + restart service)
│   └── health_check.sh         # Quick health check of all components
├── src/
│   └── valentine/
│       ├── __init__.py
│       ├── main.py             # Entry point — spawns all processes (spawn mode), supervisor
│       ├── config.py           # Settings, env vars, constants
│       ├── models.py           # Shared data models (IncomingMessage, TaskResult, AgentTask, RoutingDecision, etc.)
│       ├── utils.py            # Shared utilities (HTTP client, retry logic, logging setup)
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── provider.py     # LLM provider abstraction + fallback chain
│       │   ├── quota.py        # QuotaTracker — sliding-window rate limit tracking
│       │   ├── groq.py         # Groq API client
│       │   ├── cerebras.py     # Cerebras API client
│       │   └── sambanova.py    # SambaNova API client
│       ├── bus/
│       │   ├── __init__.py
│       │   └── redis_bus.py    # Redis Streams + pub/sub wrapper, health checks, reconnect
│       ├── orchestrator/
│       │   ├── __init__.py
│       │   └── zeroclaw.py     # Intent router
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── base.py         # BaseAgent ABC (lifecycle, heartbeat, error handling)
│       │   ├── codesmith.py    # Code & DevOps agent
│       │   ├── oracle.py       # Research & reasoning agent
│       │   ├── iris.py         # Vision & image agent
│       │   ├── echo.py         # Voice agent
│       │   └── cortex.py       # Memory agent
│       └── nexus/
│           ├── __init__.py
│           ├── adapter.py      # PlatformAdapter ABC
│           └── telegram.py     # Telegram adapter
├── workspace/                  # Shared agent workspace
├── tests/
│   ├── test_zeroclaw.py
│   ├── test_redis_bus.py
│   ├── test_llm_provider.py
│   ├── test_codesmith.py
│   ├── test_oracle_agent.py
│   ├── test_iris.py
│   ├── test_echo.py
│   ├── test_cortex.py
│   ├── test_nexus.py
│   └── test_integration.py    # Multi-agent chain tests
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-03-21-zeroclaw-architecture-design.md
```

---

## 6. Deployment

### On Oracle Cloud VM (1 OCPU, 6GB RAM, ARM64):
- **Redis:** Installed via `apt install redis-server` (no Docker overhead, ~20MB RAM)
- **Qdrant:** Run in Docker (ARM-compatible image, ~350-500MB RAM)
- **Swap:** 2GB swap file recommended as safety net for transient spikes
- **Valentine:** Single systemd service that spawns all agent processes
- **Process management:** `main.py` uses `multiprocessing` with `spawn` start method to avoid fork + asyncio issues. Supervisor monitors agent heartbeats via Redis, restarts unresponsive agents.
- **Logging:** Structured JSON logging -> journalctl, with optional log drain

### Health Monitoring
- Each agent publishes a heartbeat to `pubsub:heartbeat` every 30 seconds
- Supervisor checks heartbeats — if an agent misses 3 consecutive beats, it's restarted
- Optional: lightweight HTTP health endpoint on localhost:8080 for external monitoring

### Graceful Shutdown
- `main.py` handles SIGTERM/SIGINT
- Signals all agents to finish current task (max 30s timeout)
- Agents flush pending Redis acknowledgements
- Cortex flushes pending memory writes to Qdrant
- Supervisor waits for all agents, then exits cleanly
- systemd `TimeoutStopSec=45` to allow graceful shutdown before kill

### Systemd Service:
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
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=/home/ubuntu/valentine/.env

[Install]
WantedBy=multi-user.target
```

---

## 7. Shared Data Models

Core dataclasses used across all components:

```python
@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    user_id: str
    platform: str               # "telegram", "whatsapp", etc.
    content_type: str           # "text", "photo", "voice", "document"
    text: str | None
    media_path: str | None      # Local path to downloaded media
    timestamp: datetime

@dataclass
class RoutingDecision:
    intent: str
    agent: str
    priority: str               # "normal", "urgent"
    context_needed: bool
    chain: list[str] | None     # ["iris", "codesmith"] for multi-agent
    params: dict
    memory_context: list[str]   # Injected by Cortex sync fetch

@dataclass
class AgentTask:
    task_id: str
    agent: str
    routing: RoutingDecision
    message: IncomingMessage
    previous_results: list[str] # Output from earlier agents in a chain

@dataclass
class TaskResult:
    task_id: str
    agent: str
    success: bool
    content_type: str           # "text", "image", "voice", "document"
    text: str | None
    media_path: str | None
    error: str | None
    processing_time_ms: int
```
