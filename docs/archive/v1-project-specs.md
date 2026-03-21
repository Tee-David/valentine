# Valentine AI — Project Specifications

## 1. Overview

**Valentine** is a highly capable, "Jarvis-level" personal AI assistant operating primarily through Telegram. It is designed to be proactive, memory-persistent, multimodal, and agentic. It is hosted on an Oracle Cloud ARM64 VM.

## 2. Core Architecture

- **Host System:** Oracle Cloud VM (Ubuntu 22.04 ARM64, 4 OCPUs, 24GB RAM)
- **Primary Interface:** Telegram Bot API (`python-telegram-bot` v21.11.1)
- **Primary LLM (The Brain):** Google Gemini 2.5 Flash via `langchain-google-genai`
- **Fallback LLM:** Local Qwen2.5:1.5b via `Ollama`
- **Memory Subsystem:** `mem0ai` backed by a local Qdrant Vector DB, using local `nomic-embed-text` for embeddings.

## 3. Implemented Capabilities (Phases 1-5)

- **Text & Reasoning:** Processes complex text queries with deep reasoning, falling back to local Ollama if Gemini is unreachable.
- **Voice Processing (STT):** Transcribes voice notes using local OpenAI `Whisper` (tiny model) and `pydub`/`ffmpeg`, then responds.
- **Vision (Multimodality):** Analyzes photos physically sent to the chat using Gemini Vision.
- **Document Analysis:** Reads and analyzes PDF files sent to the chat (using `pdftotext`).
- **Infinite Memory:** Uses `mem0` to store user facts, onboarding data (name, role, communication style), and conversation history. This context is injected into every prompt.
- **Autonomous Agentic Tools:**
  - **Live Web Search:** Uses `duckduckgo-search` to answer factual queries about current events.
  - **Server Diagnostics:** Uses `psutil` to check the VM's CPU, RAM, and Disk space.

## 4. DevOps & Coding AI Expansion (Phase 6 - In Progress)

Valentine is being upgraded into an "AI IDE" capable of managing infrastructure and code.

- **Shell Execution (`execute_shell_command`):** Allows Valentine to run bash scripts (like `skills.sh`) and terminal commands directly on the host VM (currently sandboxed to `/home/ubuntu`).
- **File Management (`read_local_file`, `write_local_file`):** Allows Valentine to read source code, config files, and write updates directly to the file system.
- **GitHub Integration (Planned):** Full read/write access to repositories, Issues, and PRs via `PyGitHub`.
- **Cloud Management (Planned):** API hooks to manage and monitor Vercel and Render deployments.

## 5. Security & Isolation

- Valentine currently runs as the `ubuntu` user as a systemd service (`valentine.service`).
- SSH access is restricted to specific IPs using Oracle Security Lists.
- The `OPENAI_API_KEY` conflict for mem0 embeddings was resolved by configuring mem0 to use the local Ollama provider for embeddings.
- File writes are currently restricted via code to the `/home/ubuntu/` directory structure to prevent system file corruption.
