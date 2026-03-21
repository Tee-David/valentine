# Valentine v2 (ZeroClaw) — Build Checklist

## Phase 0: Project Scaffold
- [x] Initialize Python project with pyproject.toml (dependencies, metadata)
- [x] Create src/valentine/ package structure
- [x] Create .env template with all required keys
- [x] Set up config.py (load env vars, define constants, model mappings)
- [x] Create shared data models (models.py — IncomingMessage, TaskResult, AgentTask, etc.)

## Phase 1: LLM Provider Layer
- [x] Build LLM provider abstraction (provider.py — unified interface for all APIs)
- [x] Implement Groq client (groq.py — chat completions, streaming, Whisper STT)
- [x] Implement Cerebras client (cerebras.py — chat completions, streaming)
- [x] Implement SambaNova client (sambanova.py — chat completions, multimodal)
- [x] Implement fallback chain logic (Groq -> Cerebras -> SambaNova -> retry queue)
- [ ] Test all three providers with basic prompts

## Phase 2: Redis Message Bus
- [x] Set up Redis connection wrapper (redis_bus.py)
- [x] Implement pub/sub channel management (subscribe, publish, listen)
- [x] Implement Redis Streams for task queues (add task, read task, acknowledge)
- [x] Define channel naming conventions (zeroclaw.route, agent.{name}.task, agent.{name}.result)
- [x] Add connection health checks and auto-reconnect

## Phase 3: Base Agent Framework
- [x] Build BaseAgent ABC (agents/base.py)
  - [x] Lifecycle: startup, listen for tasks, process, publish result, shutdown
  - [x] LLM provider integration with fallback
  - [x] System prompt management
  - [x] Error handling and logging
  - [x] Health check endpoint
- [x] Build agent process supervisor in main.py (spawn, monitor, restart on crash)

## Phase 4: ZeroClaw — Intent Router
- [x] Build intent classification system prompt (structured JSON output)
- [x] Implement routing logic (intent -> agent mapping)
- [x] Implement multi-agent chaining (pipeline execution for complex requests)
- [x] Add Cortex context injection hook (fetch memories before routing)
- [x] Handle unknown/ambiguous intents (default to Oracle)
- [ ] Test routing with sample messages across all intent types

## Phase 5: Cortex — Memory Agent
- [x] Install and configure Mem0
- [x] Set up Qdrant (local Docker or direct install)
- [x] Configure embedding model (local sentence-transformer or API)
- [x] Implement memory storage (add, search, update, delete)
- [x] Implement automatic memory extraction from conversations
- [x] Implement context injection (query relevant memories for incoming requests)
- [x] Implement user profiling (preferences, style, projects)
- [ ] Test memory round-trip: store -> retrieve -> inject

## Phase 6: Oracle — Research & Reasoning Agent
- [x] Implement system prompt (world-class research analyst persona)
- [x] Integrate DuckDuckGo web search
- [x] Implement URL fetching + content extraction (readability parsing)
- [x] Implement multi-step reasoning chains
- [x] Implement summarization for long content
- [x] Handle casual conversation / general chat
- [ ] Test with research queries, chat, and summarization tasks

## Phase 7: CodeSmith — Code & DevOps Agent
- [x] Implement system prompt (senior full-stack engineer persona)
- [x] Implement sandboxed shell execution (subprocess, timeout, output capture)
- [x] Implement file read/write with directory restrictions
- [x] Implement GitHub integration via API (clone, commit, push, PRs)
- [x] Implement package management (npm, pip, apt awareness)
- [x] Add command allowlist/denylist for security
- [ ] Test with code generation, shell commands, file operations

## Phase 8: Iris — Vision & Image Agent
- [x] Implement system prompt (precision vision analyst persona)
- [x] Integrate multimodal model (Qwen-VL via SambaNova)
- [x] Implement image analysis and visual Q&A
- [x] Implement OCR (text extraction from images)
- [x] Integrate image generation API (Pollinations.ai or equivalent)
- [x] Implement screenshot-to-code pipeline (analyze -> describe -> pass to CodeSmith)
- [ ] Test with photos, screenshots, OCR, and generation requests

## Phase 9: Echo — Voice Agent
- [x] Implement system prompt (voice assistant persona)
- [x] Implement Whisper STT integration (Groq API)
- [x] Implement TTS integration (local Edge TTS or similar)
- [x] Build voice message handler (Ogg/Opus parsing)
- [ ] Test voice-in -> text-out
- [ ] Test voice-in -> voice-out (soft free)
- [x] Handle Telegram OGG voice notes -> WAV conversion
- [x] Implement voice transcription -> ZeroClaw re-routing pipeline
- [ ] Test with voice notes of varying lengths and languages

## Phase 10: Nexus — Telegram Platform Adapter
- [x] Implement PlatformAdapter ABC (adapter.py)
- [x] Build Telegram adapter (telegram.py)
  - [x] Message receiving (text, photo, voice, document, video)
  - [x] Message sending (text with Markdown, images, voice, documents)
  - [x] Media download handling (Telegram file API)
  - [x] Typing indicators during processing
  - [x] Rate limiting and message queue buffering
  - [x] Error handling (network issues, API limits)
- [x] Wire Nexus <-> ZeroClaw communication via Redis
- [ ] Test end-to-end: Telegram message -> ZeroClaw -> Agent -> Response

## Phase 11: Telegram Bot Integration
- [x] Install python-telegram-bot
- [x] Implement bot setup (webhook or long polling)
- [x] Implement message handler (text, photo, voice, document)
- [x] Translate Telegram messages to `IncomingMessage` schema
- [x] Implement agent response listening (subscribe to Redis `agent.response`)
- [x] Send responses back to Telegram users
- [x] Implement typing indicators during processing
- [ ] Test end-to-end functionality

## Phase 12: Integration & Wiring
- [x] Build main.py entry point (spawns all processes)
- [x] Implement process supervisor (health checks, auto-restart)
- [x] Wire all agents to Redis bus
- [x] Wire Cortex context injection into ZeroClaw routing
- [ ] End-to-end test: every agent type via Telegram
- [ ] Test multi-agent chaining (e.g., Iris -> CodeSmith pipeline)
- [ ] Test API fallback (simulate Groq down -> Cerebras picks up)

## Phase 12: Deployment Config
- [x] Create docker-compose.yml (Redis + Qdrant)
- [x] Create systemd service file for Valentine
- [x] Create deployment script (install deps, start services, verify health)
- [ ] Configure structured JSON logging
- [/] Test on Oracle Cloud VM specs (1 OCPU, 6GB RAM, ARM64)

## Phase 13: Hardening
- [x] Add graceful shutdown handling (SIGTERM -> clean Redis disconnect)
- [x] Add request timeout handling per agent
- [x] Add API rate limit tracking per provider
- [x] Add basic health check endpoint (HTTP on localhost for monitoring)
- [ ] Stress test: rapid message bursts
- [ ] Memory leak check: run for extended period, monitor RAM
