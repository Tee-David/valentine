# Valentine AI Upgrade Checklist

This task checklist tracks the implementation of advanced features to make Valentine the smartest and most efficient Telegram AI agent possible.

## Phase 1: The Brain-Swap (Speed & Intelligence)
- [x] Refactor `agent.py` to support both `langchain-google-genai` and local `Ollama`
- [x] Implement fallback logic: try Gemini first, if API fails/rate-limits, fall back to Qwen
- [x] Integrate Gemini API key via environment variables
- [x] Test hybrid inference speed and failover recovery

## Phase 2: Auditory Processing (Voice Notes)
- [x] Add `pydub` and `ffmpeg` dependencies to the server
- [x] Integrate OpenAI Whisper (or similar STT API) to transcribe incoming voice notes
- [x] Create a Telegram MessageHandler specifically for `filters.VOICE`
- [ ] (Optional) Integrate TTS to reply with voice messages

## Phase 3: Infinite Memory (Mem0 Integration)
- [x] Add `mem0ai` dependency to the server
- [x] Integrate Mem0 client to store user facts, preferences, and long-term memories
- [x] Inject retrieved Mem0 context into the Gemini/Ollama prompt
- [x] Test Valentine's ability to recall long-term preferences

## Phase 4: Multimodality (Vision)
- [x] Update Telegram handlers to accept `filters.PHOTO` and `filters.Document.PDF`
- [x] Implement logic to download the media to the server temporarily
- [x] Pass the media to the Gemini API vision model along with the user's prompt
- [x] Test image and document comprehension

## Phase 5: Agentic Tool Execution
- [x] Implement autonomous tool usage (Web Search, Server Status)
- [x] Integrate conversational memory with tools via `mem0`
- [x] Create an onboarding experience for first-time use
- [x] Develop a distinct "Jarvis-like" personalized system prompt

## Phase 6: DevOps & Coding Agent Capabilities (The "AI IDE" Level)
- [ ] **GitHub Integration:** Read repositories, create PRs, review code, and track issues.
- [ ] **CLI Access:** Allow Valentine to execute secure, sandboxed shell commands to manage your environments.
- [ ] **Vercel / Render / Cloud Management:** Integrate APIs to trigger deployments, check build logs, or rollback versions.
- [ ] **File System Editing:** Enable Valentine to read, write, and patch files in specific project directories directly.
- [ ] **Workspace Context:** Ensure the agent maintains an understanding of your current active coding projects.
- [ ] **External Skill Execution:** Build a LangChain tool that allows Valentine to execute external bash scripts like `skills.sh`, enabling infinite extensibility without modifying the core python code.
