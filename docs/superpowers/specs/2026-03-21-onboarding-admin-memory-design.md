# Valentine v2 — Onboarding, Admin Controls & Persistent Memory

**Date:** 2026-03-21
**Status:** Design

---

## 1. Overview

Add multi-user access control, interactive onboarding, admin management, and continuous memory learning to Valentine's Telegram bot. The bot becomes a private, whitelist-only assistant where the admin (owner) controls who can use it and what capabilities each user has.

---

## 2. User Access Model

### 2.1 Access Tiers

| Tier | Description | Capabilities |
|------|-------------|-------------|
| **Admin** | Bot owner. Full control. | All agents, all commands, user management, shell exec, skills |
| **Pro** | Approved power user. | All agents except shell_exec. No admin commands. |
| **Basic** | Approved limited user. | Oracle only. No code exec, no skills, no image gen. |
| **Pending** | Sent /start, awaiting approval. | Nothing — sees "waiting for approval" message. |
| **Revoked** | Previously approved, access removed. | Nothing — sees "access revoked" message. |

### 2.2 Capability Flags

Stored as a JSON array per user. Admin always has all.

```
oracle, codesmith, iris, echo, nexus,
web_search, shell_exec, skills, image_gen
```

Default sets:
- **Admin**: all capabilities
- **Pro**: all except `shell_exec`
- **Basic**: `oracle` only
- Admin can customize per user via `/grant` and `/deny`

### 2.3 Admin Identification

The admin user ID is set via environment variable `ADMIN_TELEGRAM_ID` in `.env`. This user is auto-approved with admin tier on first `/start`. Only one admin.

---

## 3. SQLite Database

### 3.1 Location

`/opt/valentine/data/valentine.db` (configurable via `Settings.db_path`)

### 3.2 Schema

All Telegram user IDs must be converted to `str()` before storage or lookup. Telegram IDs are integers but we store them as TEXT for consistency.

```sql
CREATE TABLE users (
    user_id             TEXT PRIMARY KEY,
    telegram_username   TEXT,
    display_name        TEXT,
    access_level        TEXT NOT NULL DEFAULT 'pending',
    capabilities        TEXT NOT NULL DEFAULT '[]',
    onboarding_complete INTEGER NOT NULL DEFAULT 0,
    preferences         TEXT NOT NULL DEFAULT '{}',
    admin_notified      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    granted_by          TEXT,
    granted_at          TEXT,
    revoked_at          TEXT
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_user TEXT,
    details     TEXT,
    timestamp   TEXT NOT NULL
);
```

The `preferences` column stores onboarding data as JSON:
```json
{
    "timezone": "Africa/Lagos",
    "role": "Developer/Engineer",
    "role_detail": "backend systems",
    "experience": "expert",
    "usage_intent": ["coding", "research", "productivity"],
    "tools": "Python, TypeScript, Docker, Redis",
    "comm_style": "concise",
    "comm_tone": "casual",
    "proactivity": "proactive",
    "personal": "interested in AI and music production"
}
```

This ensures preferences survive even if mem0/Qdrant is down. mem0 stores the same data as natural language facts for vector search.

The `admin_notified` flag prevents spamming the admin with repeated access request notifications for the same pending user.

### 3.3 UserDB Class

`src/valentine/db.py`

Methods:
- `get_user(user_id) -> dict | None`
- `create_user(user_id, username, display_name) -> dict`
- `approve_user(user_id, level, granted_by, capabilities) -> dict`
- `revoke_user(user_id, admin_id)`
- `update_user(user_id, **fields)`
- `set_capabilities(user_id, capabilities: list[str])`
- `add_capability(user_id, capability)`
- `remove_capability(user_id, capability)`
- `list_users(level=None) -> list[dict]`
- `has_capability(user_id, capability) -> bool`
- `is_admin(user_id) -> bool`
- `log_action(admin_id, action, target_user, details)`
- `get_audit_log(limit=50) -> list[dict]`

Thread-safe via `sqlite3.connect(..., check_same_thread=False)` with a threading lock for writes. Read-heavy workload is fine for SQLite.

**Injection points:**
- `TelegramAdapter.__init__` receives `UserDB` instance: `TelegramAdapter(bus, db)`
- `_run_bot_process()` in `main.py` creates the `UserDB` and passes it to the adapter
- ZeroClaw does NOT access SQLite directly — user capabilities are serialized into the `AgentTask` (see Section 8.1)

---

## 4. Onboarding Flow

### 4.1 Trigger

- Admin: immediately on first `/start`
- Approved user: on first message after approval
- Can be re-triggered via `/settings`

### 4.2 State Machine

Onboarding state stored in Redis key `onboarding:{user_id}` as JSON with a **7-day TTL**. If a user abandons onboarding, the stale state expires and they restart fresh on their next message.

```json
{
    "step": "name",
    "data": {},
    "started_at": "2026-03-21T12:00:00Z"
}
```

Steps in order:

```
welcome → name → timezone → role → role_detail → experience →
usage_intent → tools (conditional) → comm_style → comm_tone →
proactivity → personal → complete
```

### 4.3 Step Definitions

**Step: welcome**
```
Message: "Hey! I'm Valentine — your personal AI assistant.
         I'd love to get to know you so I can be more helpful.
         Want to do a quick intro? Takes about 2 minutes."

Buttons: [Let's go!] [Skip for now]
```
- "Let's go" → advance to `name`
- "Skip" → mark onboarding complete with defaults, go to normal chat

**Step: name**
```
Message: "What should I call you?"

Input: Free text or voice note
```
- Store in `data.name`, advance to `timezone`

**Step: timezone**
```
Message: "Where in the world are you?"

Buttons: [Africa] [Americas] [Europe] [Asia/Middle East] [Pacific/Oceania]
```
- On selection, show sub-buttons with major cities/timezones for that region
- Or accept free text ("Lagos", "EST", "GMT+1")

**Step: role**
```
Message: "What do you do, {name}?"

Buttons: [Student] [Developer/Engineer] [Designer] [Business/Entrepreneur]
         [Creative/Writer] [Researcher] [Other]
```

**Step: role_detail**
```
Message: "Nice! What's your focus area or specialty?"

Input: Free text or voice note
```

**Step: experience**
```
Message: "How technical are you?"

Buttons: [Just getting started] [Intermediate] [Advanced] [Expert]
```

**Step: usage_intent**
```
Message: "What will you mainly use me for? Pick all that apply,
         then hit Done."

Buttons (toggle, multi-select):
  [Coding] [Research] [Writing] [Learning]
  [Productivity] [Creative work] [Daily assistant] [Just vibes]
  ─────
  [Done]
```
- Track selected items in `data.usage_intent[]`
- Each tap toggles the button (add/remove checkmark)
- "Done" advances

**Step: tools** (only if "Coding" was selected in usage_intent)
```
Message: "What languages, frameworks, or tools do you use daily?"

Input: Free text or voice note
```

**Step: comm_style**
```
Message: "How do you like your responses?"

Buttons: [Short & punchy] [Balanced] [Detailed & thorough]
```

**Step: comm_tone**
```
Message: "And the tone?"

Buttons: [Casual & friendly] [Professional] [Match my energy]
```

**Step: proactivity**
```
Message: "Should I be proactive — suggest things, follow up, check in?"

Buttons: [Yes, be proactive] [Only when I ask] [Somewhere in between]
```

**Step: personal**
```
Message: "Last one — anything else you want me to know?
         Hobbies, interests, things you geek out about,
         pet peeves, anything at all. Or skip this."

Input: Free text or voice note
Buttons: [Skip]
```

**Step: complete**
```
Message: "Got it, {name}! Here's what I know about you:

         Name: {name}
         Location: {timezone}
         Role: {role} — {role_detail}
         Level: {experience}
         Uses: {usage_intent}
         Style: {comm_style}, {comm_tone}
         Proactivity: {proactivity}

         I'll remember all of this and keep learning as we chat.
         Update anytime with /settings. Now — what can I do for you?"
```

### 4.4 Voice Note Handling During Onboarding

If a user sends a voice note at any onboarding step:
1. TelegramAdapter downloads and transcribes it (using Groq Whisper directly, not routing through Echo agent)
2. The transcribed text is used as the answer for that step
3. For button-based steps, Valentine uses a quick LLM call to map the transcribed text to the closest option (e.g., "I'm an engineer" → `Developer/Engineer`)

### 4.5 /skip and /settings

- `/skip` during onboarding → mark complete with defaults, enter normal chat
- At any step, a `[Skip]` button skips just that step
- `/settings` after onboarding → shows a menu of categories to update:
  ```
  Buttons: [Name] [Timezone] [Role] [Communication style]
           [Usage preferences] [Proactivity] [Personal info]
  ```
  Tapping any re-runs just that step.

### 4.6 Data Storage

Every onboarding answer is stored in **two places**:
1. **SQLite** `users` table: `display_name` updated, all preferences stored in `preferences` JSON column
2. **mem0** via Cortex: structured natural-language facts stored for vector search and context injection

SQLite is the source of truth for structured preferences (survives Qdrant outage). mem0 is for semantic recall during conversations.

Example mem0 entries from onboarding:
```
"User's name is David"
"User is based in Lagos, Nigeria (WAT/GMT+1)"
"User is a software developer specializing in backend systems"
"User is at expert technical level"
"User mainly uses Valentine for coding, research, and productivity"
"User works with Python, TypeScript, Docker, Redis daily"
"User prefers concise responses with a casual friendly tone"
"User wants Valentine to be proactive with suggestions"
"User is interested in AI, cloud architecture, and music production"
```

---

## 5. Commands

### 5.1 User Commands (all approved users)

| Command | Description | Inline buttons? |
|---------|-------------|----------------|
| `/start` | Welcome + onboarding or access request | Yes |
| `/help` | Shows commands available to user's level | Yes — tap to run |
| `/settings` | Update preferences from onboarding | Yes — category picker |
| `/reset` | Clear own conversation history | Yes — confirm dialog |
| `/me` | Show profile, preferences, what Valentine remembers | No |
| `/forget <topic>` | Ask Valentine to forget something (searches mem0 for matching facts, deletes them, confirms what was removed) | No |
| `/skip` | Skip onboarding step or entire onboarding | No |

### 5.2 Admin Commands (admin only)

| Command | Description | Inline buttons? |
|---------|-------------|----------------|
| `/approve <user> [level]` | Approve pending user. If level omitted, shows inline buttons: `[As Pro] [As Basic]`. If level provided (`/approve 123 pro`), approves immediately. | Yes if no level |
| `/revoke <user>` | Revoke user access | Yes — confirm |
| `/users` | List all users with levels | Yes — tap user for details |
| `/setlevel <user> <level>` | Change access tier | No |
| `/grant <user> <capability>` | Add capability to user | No |
| `/deny <user> <capability>` | Remove capability from user | No |
| `/status` | Server health, agents, uptime | No |
| `/logs [n]` | Last N log lines | No |
| `/skills` | List installed skills | No |
| `/memory <user>` | View memories about a user. If arg is "clear \<user\>", wipes that user's mem0 data (with confirm button). Single `CommandHandler("memory", _cmd_memory)` with argument parsing. | Confirm for clear |
| `/broadcast <msg>` | Message all approved users | Yes — confirm |

### 5.3 Callback Handling

Inline button callbacks use a structured format:
```
callback_data = "action:param1:param2"
```

Examples:
- `onboard:next` — advance onboarding
- `onboard:skip` — skip current step
- `onboard:skip_all` — skip entire onboarding
- `onboard:select:coding` — toggle multi-select option
- `onboard:done` — finish multi-select
- `approve:12345:pro` — admin approves user as Pro
- `approve:12345:basic` — admin approves user as Basic
- `deny:12345` — admin denies user
- `confirm:reset` — confirm history clear
- `confirm:revoke:12345` — confirm user revocation
- `settings:name` — re-run name onboarding step
- `help:cmd:/status` — run a command from help menu

### 5.4 Unapproved User Flow

```
User sends /start or any message
        │
        ▼
TelegramAdapter checks SQLite
        │
   Not found / pending
        │
        ▼
Creates user record (status: pending)
        │
        ▼
Sends to user:
  "Hey! I'm Valentine, a private AI assistant.
   I've notified my admin about your request.
   You'll hear back soon!"
        │
        ▼
Sends to admin:
  "🔔 New access request:
   @username (ID: 12345)
   Sent: 2026-03-21 10:30 UTC"
  [Approve as Pro] [Approve as Basic] [Deny]
```

---

## 6. Persistent Memory & Continuous Learning

### 6.1 Memory Flow

```
Every message exchange:

User message → Agent response
                    │
                    ▼ (async, non-blocking)
            Cortex receives:
            {user_id, user_message, agent_response, chat_id}
                    │
                    ▼
            LLM extracts facts:
            "User mentioned they're switching to Rust"
            "User is frustrated with Docker networking"
                    │
                    ▼
            mem0.add(facts, user_id=user_id)
```

### 6.2 Context Injection

Before routing, ZeroClaw calls:
```python
memories = cortex.fetch_context_for_routing(message)
# Returns: ["User is David, a backend developer",
#           "User prefers Python", "User is working on Valentine"]
```

These get attached to `RoutingDecision.memory_context` and injected into the agent's prompt.

### 6.3 Memory Extraction Prompt

Cortex uses this prompt to extract facts:

```
Analyze this conversation exchange and extract facts worth remembering
about the user for future conversations. Focus on:
- Personal details (name, location, job, relationships)
- Technical preferences (languages, tools, frameworks, stack)
- Communication preferences (tone, detail level, style)
- Current projects and goals
- Opinions, likes, dislikes
- Behavioral patterns

Only extract CONCRETE facts. Skip greetings, pleasantries, and
transient queries. If nothing worth remembering, respond with "NONE".

Output one fact per line.
```

### 6.4 Preference-Aware Responses

Agent system prompts will include a user context block:

```
USER CONTEXT (from memory):
- Name: David
- Role: Software developer (backend, systems)
- Technical level: Expert
- Preferences: Concise responses, casual tone, proactive suggestions
- Current project: Valentine AI assistant
- Stack: Python, Redis, Docker, Oracle Cloud

Tailor your response to this user's profile and preferences.
```

---

## 7. Telegram Adapter Changes

### 7.1 New Handler Registration

```python
# Commands
CommandHandler("start", _cmd_start)
CommandHandler("help", _cmd_help)
CommandHandler("settings", _cmd_settings)
CommandHandler("reset", _cmd_reset)
CommandHandler("me", _cmd_me)
CommandHandler("forget", _cmd_forget)
CommandHandler("skip", _cmd_skip)
CommandHandler("approve", _cmd_approve)      # admin
CommandHandler("revoke", _cmd_revoke)        # admin
CommandHandler("users", _cmd_users)          # admin
CommandHandler("setlevel", _cmd_setlevel)    # admin
CommandHandler("grant", _cmd_grant)          # admin
CommandHandler("deny", _cmd_deny)            # admin
CommandHandler("status", _cmd_status)        # admin
CommandHandler("logs", _cmd_logs)            # admin
CommandHandler("skills", _cmd_skills)        # admin
CommandHandler("memory", _cmd_memory)        # admin
CommandHandler("broadcast", _cmd_broadcast)  # admin

# Callbacks
CallbackQueryHandler(_on_callback)

# Messages (text, photo, voice, etc. — existing)
```

### 7.2 Message Flow with Access Control

```python
async def _on_text(self, update, ctx):
    user_id = str(update.effective_user.id)

    # 1. Access check
    user = self.db.get_user(user_id)
    if not user or user["access_level"] in ("pending", "revoked"):
        return await self._handle_unapproved(update)

    # 2. Onboarding check
    if not user["onboarding_complete"]:
        return await self._handle_onboarding_step(update, user)

    # 3. Capability check happens in ZeroClaw after routing
    #    (user capabilities attached to the task)

    # 4. Normal routing
    await self.send_typing(str(update.effective_chat.id))
    await self._route(update, ContentType.TEXT, update.message.text)
```

### 7.3 Onboarding State Machine in Adapter

The TelegramAdapter manages onboarding state via Redis:
- `onboarding:{user_id}` — JSON with current step and collected data
- On each message/callback during onboarding, advance the state machine
- Voice notes during onboarding: transcribe inline (direct Groq Whisper API call, not routed through Echo)

---

## 8. ZeroClaw Changes

### 8.1 Capability Enforcement

**How capabilities flow from adapter to ZeroClaw:**

1. TelegramAdapter looks up the user's capabilities from SQLite
2. Capabilities are serialized into `IncomingMessage` via a new `user_capabilities` field (added to the model)
3. ZeroClaw reads capabilities from `task.message.user_capabilities` — no SQLite import needed in ZeroClaw's process

```python
# In TelegramAdapter._route():
msg = IncomingMessage(
    ...,
    user_capabilities=user["capabilities"],  # JSON list from SQLite
)

# In ZeroClaw.process_task():
AGENT_CAPABILITIES = {
    AgentName.ORACLE: "oracle",
    AgentName.CODESMITH: "codesmith",
    AgentName.IRIS: "iris",
    AgentName.ECHO: "echo",
    AgentName.NEXUS: "nexus",
}

user_caps = msg.user_capabilities or []
required_cap = AGENT_CAPABILITIES.get(target_agent, "oracle")

if required_cap not in user_caps:
    # Don't silently redirect — tell the user clearly
    target_agent = AgentName.ORACLE
    routing.intent = "capability_blocked"
    routing.params["blocked_agent"] = target_agent.value
    routing.params["blocked_reason"] = required_cap
```

**Oracle handles `capability_blocked` intent explicitly:**

```python
# In Oracle.process_task():
if task.routing.intent == "capability_blocked":
    blocked = task.routing.params.get("blocked_agent", "that agent")
    return TaskResult(
        task_id=task.task_id, agent=self.name, success=True,
        text=f"I'd love to help with that, but your current access level "
             f"doesn't include {blocked} capabilities. You can ask the admin "
             f"to upgrade your access if you need this feature!"
    )
```

**Model change — add `user_capabilities` to `IncomingMessage`:**

```python
@dataclass
class IncomingMessage:
    ...
    user_capabilities: list[str] = field(default_factory=list)
```

Include in `to_dict()` and `from_dict()` for serialization across Redis.

### 8.2 Pre-routing Memory Fetch

```python
async def process_task(self, task):
    msg = task.message
    # Fetch relevant memories before routing
    context_items = await self._fetch_context(msg)
    # ... rest of routing logic
    routing.memory_context = context_items
```

`_fetch_context` calls mem0 search with the user's message text and user_id.

---

## 9. Cortex Changes

### 9.1 Background Memory Extraction

**Trigger mechanism:** After every agent response, `BaseAgent.publish_result()` pushes a memory extraction task to the Cortex task stream:

```python
# In BaseAgent.publish_result():
async def publish_result(self, result: TaskResult):
    await self.bus.add_task(self.result_stream, result.to_dict())
    await self.bus.publish("agent.response", result.to_dict())

    # Fire memory extraction to Cortex (non-blocking)
    if result.success and result.text and hasattr(self, '_last_user_msg'):
        extraction_task = {
            "type": "extract_memory",
            "user_id": self._last_user_id,
            "user_message": self._last_user_msg,
            "agent_response": result.text,
        }
        await self.bus.add_task(
            self.bus.stream_name("cortex", "task"),
            extraction_task,
        )
```

BaseAgent stores `_last_user_msg` and `_last_user_id` in `listen_for_tasks()` before calling `process_task()`.

**Cortex handles the extraction task:**

```python
async def process_task(self, task_or_dict):
    # Handle raw extraction dicts (not AgentTask)
    if isinstance(task_or_dict, dict) and task_or_dict.get("type") == "extract_memory":
        return await self._extract_from_exchange(task_or_dict)
    # ... normal AgentTask handling

async def _extract_from_exchange(self, data):
    user_id = data["user_id"]
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=data["user_message"],
        agent_response=data["agent_response"],
    )
    messages = [
        {"role": "system", "content": self.system_prompt},
        {"role": "user", "content": prompt},
    ]
    extraction = await self.llm.chat_completion(messages, temperature=0.1)
    if extraction and "NONE" not in extraction.upper():
        for fact in extraction.strip().split("\n"):
            fact = fact.strip("- ").strip()
            if len(fact) > 5:
                # Use asyncio.to_thread since mem0.add() is blocking I/O
                await asyncio.to_thread(self.memory.add, fact, user_id=user_id)
```

**Important:** All `mem0` calls (`memory.add()`, `memory.search()`) are blocking I/O (Qdrant HTTP + embedding computation). They MUST be wrapped in `asyncio.to_thread()` to avoid blocking the event loop.

### 9.2 Onboarding Memory Storage

Direct method for storing onboarding facts:

```python
async def store_onboarding_facts(self, user_id, onboarding_data: dict):
    """Store structured facts from onboarding."""
    facts = []
    if name := onboarding_data.get("name"):
        facts.append(f"User's name is {name}")
    if tz := onboarding_data.get("timezone"):
        facts.append(f"User is based in {tz}")
    if role := onboarding_data.get("role"):
        detail = onboarding_data.get("role_detail", "")
        facts.append(f"User is a {role}" + (f" specializing in {detail}" if detail else ""))
    # ... etc for all fields
    for fact in facts:
        await asyncio.to_thread(self.memory.add, fact, user_id=user_id)
```

---

## 10. Config Changes

Add to `Settings`:

```python
admin_telegram_id: str = Field(default="")
db_path: str = Field(default="/opt/valentine/data/valentine.db")
```

Add to `.env`:
```
ADMIN_TELEGRAM_ID=<your telegram user id>
```

---

## 11. File Changes Summary

| File | Change |
|------|--------|
| `src/valentine/db.py` | **NEW** — SQLite UserDB class |
| `src/valentine/nexus/telegram.py` | **MAJOR** — commands, callbacks, onboarding state machine, access control, UserDB injection |
| `src/valentine/orchestrator/zeroclaw.py` | **MODERATE** — capability checking from `msg.user_capabilities`, mem0 context fetch |
| `src/valentine/agents/cortex.py` | **MODERATE** — background extraction via `extract_memory` task type, onboarding storage, `asyncio.to_thread` for mem0 calls |
| `src/valentine/agents/base.py` | **MINOR** — store `_last_user_msg`/`_last_user_id`, fire Cortex extraction task in `publish_result()` |
| `src/valentine/models.py` | **MINOR** — add `user_capabilities: list[str]` to `IncomingMessage`, update `to_dict`/`from_dict` |
| `src/valentine/main.py` | **MINOR** — create `UserDB` in `_run_bot_process()`, pass to `TelegramAdapter` |
| `src/valentine/config.py` | **MINOR** — add `admin_telegram_id`, `db_path` |
| `.env` | **MINOR** — add `ADMIN_TELEGRAM_ID` |

---

## 12. Edge Cases

- **Admin sends /start for the first time**: auto-approved as admin, enters onboarding immediately
- **User sends messages while pending**: gets "waiting for approval" every time, no spam to admin (notify once)
- **User sends voice during onboarding**: transcribed inline via Groq Whisper, transcription used as text answer for current step
- **User sends photo during onboarding**: replied with "Let's finish getting to know each other first! You can send photos after onboarding." Photo is NOT queued (too complex, low value).
- **User sends document/video during onboarding**: same as photo — polite redirect to finish onboarding first
- **All non-text message handlers** (`_on_photo`, `_on_voice`, `_on_document`, `_on_video`) must check onboarding state. Voice is handled specially (transcription). All others redirect.
- **Onboarding interrupted** (bot restarts): Redis state persists (7-day TTL), resumes at last step
- **Admin revokes user mid-conversation**: next message gets "access revoked"
- **Two admins?**: Not supported. Single admin via env var. Could be extended to a list later.
- **User blocked the bot then unblocks**: /start works again, keeps old data if not revoked
