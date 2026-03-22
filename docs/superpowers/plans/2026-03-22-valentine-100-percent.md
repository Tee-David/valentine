# Valentine 100% Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every capability Valentine claims to have actually work — no fakes, no silent crashes, real error handling, real tests.

**Architecture:** Three phases — (1) Safety & stability first (stop crashes), (2) Wire up real functionality (make claims true), (3) Tests (safety net for the future). Each task produces a working commit.

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio, httpx (for real API calls), existing Redis/Qdrant/mem0 stack.

---

## Phase 1: Safety & Stability

### Task 1: Safe JSON parsing helper

All agents that parse LLM JSON responses (ZeroClaw, CodeSmith, Nexus, Iris) use fragile inline parsing that crashes on malformed output. Extract a shared helper.

**Files:**
- Create: `src/valentine/utils.py` (add to existing if it exists, otherwise create)
- Test: `tests/test_utils.py`

- [ ] **Step 1: Write failing tests for safe_parse_json**

```python
# tests/test_utils.py
from valentine.utils import safe_parse_json


class TestSafeParseJson:
    def test_valid_json(self):
        assert safe_parse_json('{"a": 1}') == {"a": 1}

    def test_markdown_wrapped(self):
        assert safe_parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_text_before_json(self):
        result = safe_parse_json('Sure, here you go: {"tool": "weather"}')
        assert result == {"tool": "weather"}

    def test_invalid_json_returns_none(self):
        assert safe_parse_json("This is not JSON at all") is None

    def test_empty_string(self):
        assert safe_parse_json("") is None

    def test_nested_json(self):
        result = safe_parse_json('{"actions": [{"type": "shell", "cmd": "ls"}]}')
        assert result["actions"][0]["type"] == "shell"

    def test_json_array(self):
        result = safe_parse_json('[{"type": "respond"}]')
        assert isinstance(result, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_utils.py -v`
Expected: ImportError — `safe_parse_json` doesn't exist yet

- [ ] **Step 3: Implement safe_parse_json**

Add to `src/valentine/utils.py`:

```python
import json
import re
import logging

logger = logging.getLogger(__name__)


def safe_parse_json(text: str) -> dict | list | None:
    """Parse JSON from LLM output, handling markdown fences and preamble.

    Returns the parsed object, or None if no valid JSON found.
    """
    if not text or not text.strip():
        return None

    # Strip markdown code fences
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try extracting first JSON object or array from the text
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        match = re.search(pattern, cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_utils.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Replace inline JSON parsing in ZeroClaw**

In `src/valentine/orchestrator/zeroclaw.py`, replace lines 170-171:
```python
# Before:
clean_text = response_text.replace("```json", "").replace("```", "").strip()
data = json.loads(clean_text)

# After:
from valentine.utils import safe_parse_json
data = safe_parse_json(response_text)
if data is None:
    raise ValueError(f"LLM returned unparseable response: {response_text[:200]}")
```

- [ ] **Step 6: Replace inline JSON parsing in Nexus**

In `src/valentine/agents/nexus.py`, replace lines 100-103:
```python
# Before:
clean_text = response_text.replace("```json", "").replace("```", "").strip()
try:
    data = json.loads(clean_text)

# After:
from valentine.utils import safe_parse_json
data = safe_parse_json(response_text)
if data is not None and isinstance(data, dict) and "tool" in data:
```

Remove the `except json.JSONDecodeError: pass` block (line 136-137) — no longer needed.

- [ ] **Step 7: Replace inline JSON parsing in CodeSmith**

In `src/valentine/agents/codesmith.py`, replace the JSON parsing section with a call to `safe_parse_json`. Same pattern.

- [ ] **Step 8: Commit**

```bash
git add src/valentine/utils.py tests/test_utils.py src/valentine/orchestrator/zeroclaw.py src/valentine/agents/nexus.py src/valentine/agents/codesmith.py
git commit -m "feat: add safe_parse_json helper, replace fragile inline JSON parsing"
```

---

### Task 2: Harden Cortex graceful degradation

**Files:**
- Modify: `src/valentine/agents/cortex.py:7` (lazy import), `:42-46` (init), `:142-144` (process_task)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cortex.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.check_health = AsyncMock(return_value=True)
    bus.get_history = AsyncMock(return_value=[])
    bus.append_history = AsyncMock()
    bus.stream_name = MagicMock(return_value="valentine:cortex:task")
    bus.ROUTER_STREAM = "valentine:router:task"
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.provider_name = "groq"
    llm.chat_completion = AsyncMock(return_value="nothing to extract")
    return llm


class TestCortexGracefulDegradation:
    @patch("valentine.agents.cortex.Memory", None)
    def test_cortex_survives_no_memory(self, mock_llm, mock_bus):
        """Cortex should not crash when mem0 is unavailable."""
        from valentine.agents.cortex import CortexAgent
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        assert agent.memory is None

    @pytest.mark.asyncio
    @patch("valentine.agents.cortex.Memory", None)
    async def test_process_task_without_memory(self, mock_llm, mock_bus, sample_text_message):
        """process_task should return graceful message when memory is None."""
        from valentine.agents.cortex import CortexAgent
        from valentine.models import AgentTask, RoutingDecision, AgentName
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.CORTEX,
            routing=RoutingDecision(intent="search_memory", agent=AgentName.CORTEX),
            message=sample_text_message,
        )
        result = await agent.process_task(task)
        assert result.success is True  # Should NOT return success=False
        assert "unavailable" in result.text.lower() or "not available" in result.text.lower()

    @pytest.mark.asyncio
    @patch("valentine.agents.cortex.Memory", None)
    async def test_store_memory_without_memory(self, mock_llm, mock_bus, sample_text_message):
        """store_memory intent should degrade gracefully."""
        from valentine.agents.cortex import CortexAgent
        from valentine.models import AgentTask, RoutingDecision, AgentName
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t2", agent=AgentName.CORTEX,
            routing=RoutingDecision(intent="store_memory", agent=AgentName.CORTEX),
            message=sample_text_message,
        )
        result = await agent.process_task(task)
        assert result.success is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cortex.py -v`
Expected: `test_process_task_without_memory` FAILS (returns success=False with error)

- [ ] **Step 3: Fix Cortex — lazy import and graceful degradation**

In `src/valentine/agents/cortex.py`:

1. Remove the top-level `from mem0 import Memory` at line 7. Move the import inside the existing `try` block at line 42:
```python
# Line 7: DELETE `from mem0 import Memory`
# Lines 42-46 already have try/except — just move the import into the try block:
try:
    from mem0 import Memory  # lazy import
    self.memory = Memory.from_config(mem0_config)
except Exception as e:
    logger.warning(f"Memory layer unavailable (non-fatal): {e}")
    self.memory = None
```

2. Change lines 142-144 in `process_task`:
```python
# Before:
if not self.memory:
    return TaskResult(task_id=task.task_id, agent=self.name, success=False,
                     error="Memory layer uninitialized")

# After:
if not self.memory:
    return TaskResult(
        task_id=task.task_id, agent=self.name, success=True,
        text="Memory is temporarily unavailable. I'll still work, "
             "but I won't remember things across conversations right now."
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cortex.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/cortex.py tests/test_cortex.py
git commit -m "fix: Cortex graceful degradation when mem0/Qdrant unavailable"
```

---

### Task 3: Error boundary in BaseAgent

Raw errors from agents sometimes leak internal details (API URLs, tracebacks) to users. The `publish_result` in BaseAgent already sanitizes text, but the error path in `listen_for_tasks` creates raw error strings.

**Files:**
- Modify: `src/valentine/agents/base.py:121-131`
- Test: `tests/test_base_agent.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_base_agent.py
import pytest
from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, RoutingDecision


class TestErrorSanitization:
    def test_raw_url_stripped_from_error(self):
        """Internal API URLs should not appear in user-facing errors."""
        result = TaskResult(
            task_id="t1", agent=AgentName.ORACLE, success=False,
            error="Connection failed: https://api.groq.com/openai/v1/chat/completions returned 429",
        )
        from valentine.security import sanitise_output
        sanitized = sanitise_output(result.error)
        assert "api.groq.com" not in sanitized

    def test_traceback_stripped_from_error(self):
        result = TaskResult(
            task_id="t2", agent=AgentName.ORACLE, success=False,
            error='Traceback (most recent call last):\n  File "foo.py", line 42\nKeyError: "x"',
        )
        from valentine.security import sanitise_output
        sanitized = sanitise_output(result.error)
        assert "Traceback" not in sanitized
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_base_agent.py -v`
Expected: May fail if `sanitise_output` doesn't strip URLs/tracebacks from errors

- [ ] **Step 3: Update sanitise_output in security.py**

In `src/valentine/security.py`, ensure `sanitise_output` also handles error-like strings:

```python
def sanitise_output(text: str) -> str:
    """Remove accidentally leaked secrets, URLs, and tracebacks from output."""
    if not text:
        return text
    # ... existing secret redaction ...

    # Strip internal API URLs
    text = re.sub(r"https?://[^\s]+", "[internal-url]", text)

    # Strip tracebacks
    if "Traceback (most recent call last)" in text:
        text = "An internal error occurred. Please try again."

    return text
```

- [ ] **Step 4: Ensure listen_for_tasks sanitizes errors before publishing**

In `src/valentine/agents/base.py`, the error path at line 130 passes `str(e)` directly. The `publish_result` method (line 73-78) already sanitizes, so this should be handled. Verify by reading the code.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_base_agent.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/valentine/security.py tests/test_base_agent.py
git commit -m "fix: sanitize internal URLs and tracebacks from user-facing errors"
```

---

## Phase 2: Real Functionality

### Task 4: Rewrite Nexus with real free APIs

Replace hardcoded mock data with real API calls. Use free, no-auth-required APIs:
- **Weather**: Open-Meteo (free, no key needed, `https://api.open-meteo.com/v1/forecast`)
- **Crypto**: CoinGecko free API (no key needed, `https://api.coingecko.com/api/v3/simple/price`)
- **Time/Date**: WorldTimeAPI (free, `https://worldtimeapi.org/api/timezone`)

**Files:**
- Rewrite: `src/valentine/agents/nexus.py`
- Test: `tests/test_nexus.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_nexus.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.get_history = AsyncMock(return_value=[])
    bus.append_history = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.provider_name = "groq"
    return llm


class TestNexusToolExecution:
    @pytest.mark.asyncio
    @pytest.mark.network
    async def test_weather_returns_real_data(self, mock_llm, mock_bus):
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("get_weather", {"location": "London"})
        assert "Mock" not in result
        assert "temperature" in result.lower() or "°" in result or "error" in result.lower()

    @pytest.mark.asyncio
    @pytest.mark.network
    async def test_crypto_returns_real_data(self, mock_llm, mock_bus):
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("get_crypto_price", {"symbol": "BTC"})
        assert "Mock" not in result
        assert "$" in result or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self, mock_llm, mock_bus):
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("nonexistent_tool", {})
        assert "not found" in result.lower() or "not available" in result.lower()

    @pytest.mark.asyncio
    async def test_weather_handles_api_failure(self, mock_llm, mock_bus):
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        with patch("valentine.agents.nexus.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(side_effect=Exception("Network error"))
            result = await agent._execute_tool("get_weather", {"location": "Mars"})
            assert "error" in result.lower() or "couldn" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_nexus.py -v`
Expected: `test_weather_returns_real_data` and `test_crypto_returns_real_data` FAIL (contain "Mock")

- [ ] **Step 3: Rewrite _execute_tool with real APIs**

Replace the `_execute_tool` method in `src/valentine/agents/nexus.py`:

```python
import httpx

# Add to class:
async def _execute_tool(self, tool_name: str, params: dict) -> str:
    try:
        if tool_name == "get_weather":
            return await self._get_weather(params)
        elif tool_name == "get_crypto_price":
            return await self._get_crypto_price(params)
        elif tool_name == "get_time":
            return await self._get_time(params)
        else:
            return f"Tool '{tool_name}' is not available."
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {e}")
        return f"I couldn't fetch that data right now. Please try again in a moment."

async def _get_weather(self, params: dict) -> str:
    """Fetch real weather from Open-Meteo (free, no API key)."""
    location = params.get("location", "London")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Step 1: Geocode the location name
        geo_resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1},
        )
        geo_data = geo_resp.json()
        results = geo_data.get("results")
        if not results:
            return f"Couldn't find location '{location}'."

        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        place_name = results[0].get("name", location)
        country = results[0].get("country", "")

        # Step 2: Get current weather
        weather_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "temperature_unit": "celsius",
            },
        )
        weather = weather_resp.json().get("current", {})

        temp_c = weather.get("temperature_2m", "N/A")
        temp_f = round(temp_c * 9/5 + 32, 1) if isinstance(temp_c, (int, float)) else "N/A"
        humidity = weather.get("relative_humidity_2m", "N/A")
        wind = weather.get("wind_speed_10m", "N/A")

        return (
            f"Weather in {place_name}, {country}: "
            f"{temp_c}°C ({temp_f}°F), "
            f"humidity {humidity}%, "
            f"wind {wind} km/h"
        )

async def _get_crypto_price(self, params: dict) -> str:
    """Fetch real crypto prices from CoinGecko (free, no API key)."""
    symbol = params.get("symbol", "BTC").upper()

    # Map common symbols to CoinGecko IDs
    symbol_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "ADA": "cardano", "DOT": "polkadot", "DOGE": "dogecoin",
        "XRP": "ripple", "MATIC": "matic-network", "AVAX": "avalanche-2",
        "LINK": "chainlink", "BNB": "binancecoin", "LTC": "litecoin",
    }
    coin_id = symbol_map.get(symbol, symbol.lower())

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
        )
        data = resp.json()

    if coin_id not in data:
        return f"Couldn't find price for '{symbol}'. Try BTC, ETH, SOL, etc."

    price = data[coin_id]["usd"]
    change = data[coin_id].get("usd_24h_change")
    change_str = f" ({change:+.2f}% 24h)" if change is not None else ""

    return f"{symbol}: ${price:,.2f}{change_str}"

async def _get_time(self, params: dict) -> str:
    """Fetch current time for a timezone from WorldTimeAPI."""
    timezone = params.get("timezone", "UTC")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"https://worldtimeapi.org/api/timezone/{timezone}")
        if resp.status_code != 200:
            return f"Unknown timezone '{timezone}'. Try e.g. 'America/New_York', 'Europe/London'."
        data = resp.json()

    return f"Current time in {timezone}: {data['datetime'][:19]} ({data['abbreviation']})"
```

Also update `self.tools` dict to include `get_time` and remove "(Mock Data)" references.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_nexus.py -v`
Expected: All PASS (real API calls — may need network; the failure test uses mocking)

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/nexus.py tests/test_nexus.py
git commit -m "feat: replace Nexus mock data with real APIs (Open-Meteo, CoinGecko)"
```

---

### Task 5: Wire RAG into CodeSmith

The RAG system (`src/valentine/core/rag.py`) is a complete 319-line implementation but isn't connected to any agent. Wire it into CodeSmith so code search actually works.

**Files:**
- Modify: `src/valentine/agents/codesmith.py` — add `rag_search` action type
- Test: `tests/test_rag.py`

- [ ] **Step 1: Write tests for RAG integration**

```python
# tests/test_rag.py
import pytest
from valentine.core.rag import CodebaseRAG, CodeChunk


class TestCodeChunking:
    def test_chunk_file(self, tmp_path):
        """Chunking should split files into overlapping pieces."""
        test_file = tmp_path / "test.py"
        test_file.write_text("line1\n" * 200)  # ~1000 chars
        rag = CodebaseRAG()
        chunks = rag._chunk_file(str(test_file))
        assert len(chunks) >= 2
        assert all(isinstance(c, CodeChunk) for c in chunks)
        assert chunks[0].start_line == 1
        assert chunks[0].language == "py"

    def test_scan_directory_skips_pycache(self, tmp_path):
        """Scanner should skip __pycache__ and similar dirs."""
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "foo.py").write_text("cached")
        (tmp_path / "real.py").write_text("real code")
        rag = CodebaseRAG()
        files = rag._scan_directory(str(tmp_path))
        assert len(files) == 1
        assert "real.py" in files[0]

    def test_scan_directory_filters_extensions(self, tmp_path):
        """Only indexable extensions should be scanned."""
        (tmp_path / "code.py").write_text("python")
        (tmp_path / "binary.exe").write_text("not code")
        (tmp_path / "data.json").write_text("{}")
        rag = CodebaseRAG()
        files = rag._scan_directory(str(tmp_path))
        assert len(files) == 2  # .py and .json, not .exe
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_rag.py -v`
Expected: All PASS (these test the local-only parts, no Qdrant needed)

- [ ] **Step 3: Add rag_search action to CodeSmith**

In `src/valentine/agents/codesmith.py`, add handling for a `rag_search` action in the action execution section. When the LLM decides it needs to search the codebase, it can emit `{"type": "rag_search", "query": "auth logic"}`.

Add to the system prompt's action list:
```
- {"type": "rag_search", "query": "semantic search query"} — Search the indexed codebase
```

Add a module-level RAG singleton to avoid re-initializing the model on every call (sentence-transformers takes 5-10s to load):

```python
# At module level in codesmith.py:
_rag_instance: "CodebaseRAG | None" = None

def _get_rag():
    global _rag_instance
    if _rag_instance is None:
        from valentine.core.rag import CodebaseRAG
        _rag_instance = CodebaseRAG()
    return _rag_instance
```

Add to action routing:
```python
elif action_type == "rag_search":
    rag = _get_rag()
    query = action.get("query", "")
    results = await rag.search_formatted(query, limit=5)
    output = results if results else "No results. Codebase may not be indexed yet."
```

- [ ] **Step 4: Commit**

```bash
git add src/valentine/agents/codesmith.py tests/test_rag.py
git commit -m "feat: wire CodebaseRAG into CodeSmith for semantic code search"
```

---

### Task 6: Wire Evolution into CodeSmith error handling

The Evolution system (`src/valentine/core/evolution.py`) is 349 lines of real implementation. Wire it into CodeSmith so when a shell command fails with "command not found" or "No module named X", Valentine auto-suggests and optionally installs the missing tool.

**Files:**
- Modify: `src/valentine/agents/codesmith.py` — shell execution error handling
- Test: `tests/test_evolution.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_evolution.py
import pytest
from valentine.core.evolution import SelfEvolver


class TestSelfEvolver:
    def test_suggest_missing_python_module(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("ModuleNotFoundError: No module named 'openpyxl'")
        assert suggestion == "openpyxl"

    def test_suggest_missing_command(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("ffmpeg: command not found")
        assert suggestion == "ffmpeg"

    def test_suggest_unknown_returns_none_or_name(self):
        evolver = SelfEvolver()
        result = evolver.suggest_install("Something random happened")
        assert result is None

    def test_is_available_checks_shutil(self):
        evolver = SelfEvolver()
        # python3 should be available in test environment
        assert evolver.is_available("python3") is True

    def test_is_available_false_for_missing(self):
        evolver = SelfEvolver()
        assert evolver.is_available("definitely_not_a_real_command_xyz") is False

    def test_import_to_tool_mapping(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("No module named 'PIL'")
        assert suggestion == "pillow"
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_evolution.py -v`
Expected: All PASS

- [ ] **Step 3: Add evolution to CodeSmith shell error handling**

In `src/valentine/agents/codesmith.py`, after a shell command fails, check if Evolution can suggest a fix:

```python
# In the shell execution section, after getting stderr:
if proc.returncode != 0 and stderr:
    from valentine.core.evolution import SelfEvolver
    evolver = SelfEvolver(allow_apt=False)
    suggestion = evolver.suggest_install(stderr)
    if suggestion:
        install_info = evolver.INSTALL_MAP.get(suggestion)
        if install_info and install_info["method"] == "pip":
            # Auto-install pip packages (safe)
            install_result = await evolver.ensure_available(suggestion)
            if install_result.success:
                output += f"\n\n[Auto-installed {suggestion}. Retrying...]"
                # Retry the command
                # ...
            else:
                output += f"\n\n[Missing: {suggestion}. Install with: {install_result.message}]"
        else:
            output += f"\n\n[Missing tool: {suggestion}. {install_info}]"
```

- [ ] **Step 4: Commit**

```bash
git add src/valentine/agents/codesmith.py tests/test_evolution.py
git commit -m "feat: wire SelfEvolver into CodeSmith for auto-install on missing tools"
```

---

### Task 7: Wire DocGen into CodeSmith

The DocGen system (`src/valentine/core/docgen.py`) is 221 lines of real implementation. Wire it into CodeSmith so Valentine can actually generate and send documents.

**Files:**
- Modify: `src/valentine/agents/codesmith.py` — add `generate_document` action type
- Test: `tests/test_docgen.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_docgen.py
import pytest
from valentine.core.docgen import DocumentGenerator


class TestDocumentGenerator:
    @pytest.mark.asyncio
    async def test_generate_csv(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_csv(
            data=[["Alice", 30], ["Bob", 25]],
            headers=["Name", "Age"],
            file_name="people",
        )
        assert doc.file_type == "csv"
        assert doc.file_path.endswith(".csv")
        content = open(doc.file_path).read()
        assert "Alice" in content
        assert "Name,Age" in content

    @pytest.mark.asyncio
    async def test_generate_json(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_json({"key": "value"}, file_name="test")
        assert doc.file_type == "json"
        import json
        data = json.load(open(doc.file_path))
        assert data["key"] == "value"

    @pytest.mark.asyncio
    async def test_generate_text(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_text("Hello world", file_name="greeting")
        assert doc.file_type == "txt"
        assert open(doc.file_path).read() == "Hello world"

    @pytest.mark.asyncio
    async def test_generate_html(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_html("<h1>Test</h1>", file_name="page")
        assert doc.file_type == "html"
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_docgen.py -v`
Expected: All PASS

- [ ] **Step 3: Add generate_document action to CodeSmith**

In `src/valentine/agents/codesmith.py`, add a `generate_document` action that delegates to DocGen:

```python
elif action_type == "generate_document":
    from valentine.core.docgen import DocumentGenerator
    gen = DocumentGenerator()
    doc_type = action.get("format", "txt")
    content = action.get("content", "")
    title = action.get("title", "document")

    if doc_type == "csv":
        doc = await gen.generate_csv(
            data=action.get("data", []),
            headers=action.get("headers"),
            file_name=title,
        )
    elif doc_type == "json":
        doc = await gen.generate_json(action.get("data", {}), file_name=title)
    elif doc_type == "excel":
        doc = await gen.generate_excel(
            data=action.get("data", []),
            headers=action.get("headers"),
            file_name=title,
        )
    elif doc_type == "pdf":
        doc = await gen.generate_pdf(content, title=title, file_name=title)
    elif doc_type == "word":
        doc = await gen.generate_word(content, title=title, file_name=title)
    elif doc_type == "html":
        doc = await gen.generate_html(content, file_name=title)
    else:
        doc = await gen.generate_text(content, file_name=title)

    output = f"Generated {doc.file_type} file: {doc.file_path}"
    # Set media_path so the Telegram adapter can send the file
    final_media_path = doc.file_path
    final_file_name = doc.file_name
```

Also update the system prompt to include the `generate_document` action.

- [ ] **Step 4: Commit**

```bash
git add src/valentine/agents/codesmith.py tests/test_docgen.py
git commit -m "feat: wire DocGen into CodeSmith for document generation"
```

---

### Task 8: Wire Senses into /status and CodeSmith

The Senses system (`src/valentine/core/senses.py`) is 294 lines of real implementation. Wire it into:
1. The `/status` Telegram command (show real system info)
2. CodeSmith's system prompt (so it knows what tools are available)

**Files:**
- Modify: `src/valentine/nexus/telegram.py` — `/status` command
- Modify: `src/valentine/agents/codesmith.py` — inject environment context
- Test: `tests/test_senses.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_senses.py
import pytest
from valentine.core.senses import EnvironmentScanner, SystemInfo


class TestEnvironmentScanner:
    @pytest.mark.asyncio
    async def test_scan_system(self):
        scanner = EnvironmentScanner()
        info = await scanner._scan_system()
        assert isinstance(info, SystemInfo)
        assert info.cpu_count > 0
        assert info.python_version  # Should not be empty

    def test_scan_tools(self):
        scanner = EnvironmentScanner()
        tools = scanner._scan_tools()
        assert isinstance(tools, dict)
        assert "python3" in tools
        assert tools["python3"] is True  # python3 must be available

    @pytest.mark.asyncio
    async def test_quick_scan_returns_string(self):
        scanner = EnvironmentScanner()
        result = await scanner.quick_scan()
        assert isinstance(result, str)
        assert "System:" in result
        assert "Python:" in result
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_senses.py -v`
Expected: All PASS

- [ ] **Step 3: Update /status command to use real EnvironmentScanner**

In `src/valentine/nexus/telegram.py`, the `/status` command currently just hits the health endpoint. Enhance it:

```python
async def _cmd_status(self, update, ctx):
    # Existing health check
    # ... keep existing health check code ...

    # Add system info from Senses
    from valentine.core.senses import EnvironmentScanner
    scanner = EnvironmentScanner()
    try:
        env_summary = await scanner.quick_scan()
        status_text += f"\n\n{env_summary}"
    except Exception:
        pass  # Non-fatal

    await update.message.reply_text(status_text)
```

- [ ] **Step 4: Commit**

```bash
git add src/valentine/nexus/telegram.py src/valentine/agents/codesmith.py tests/test_senses.py
git commit -m "feat: wire EnvironmentScanner into /status and CodeSmith"
```

---

## Phase 3: Agent Tests

### Task 9: Test infrastructure — LLM and Bus mocks

Create reusable mock fixtures for all agent tests.

**Files:**
- Modify: `tests/conftest.py` — add LLM and bus mock fixtures
- Test: verify fixtures work

- [ ] **Step 1: Expand conftest.py with mock fixtures**

```python
# Add to tests/conftest.py:
from unittest.mock import AsyncMock, MagicMock
from valentine.models import IncomingMessage, ContentType

@pytest.fixture
def mock_llm():
    """Mock LLM that returns configurable responses."""
    llm = AsyncMock()
    llm.provider_name = "groq"
    llm.default_model = "test-model"
    llm.chat_completion = AsyncMock(return_value="Test response")
    llm.stream_chat_completion = AsyncMock()
    llm.image_completion = AsyncMock(return_value="I see an image of a cat.")
    llm.transcribe_audio = AsyncMock(return_value="Hello, this is a test.")
    return llm


@pytest.fixture
def mock_bus():
    """Mock Redis bus that stores history in memory."""
    bus = AsyncMock()
    bus.redis = AsyncMock()
    bus.check_health = AsyncMock(return_value=True)
    bus.close = AsyncMock()

    _history: dict[str, list] = {}

    async def get_history(chat_id, limit=20):
        return _history.get(chat_id, [])[-limit:]

    async def append_history(chat_id, role, content):
        _history.setdefault(chat_id, []).append({"role": role, "content": content})

    async def clear_history(chat_id):
        _history.pop(chat_id, None)

    bus.get_history = AsyncMock(side_effect=get_history)
    bus.append_history = AsyncMock(side_effect=append_history)
    bus.clear_history = AsyncMock(side_effect=clear_history)
    bus.stream_name = MagicMock(side_effect=lambda agent, suffix: f"valentine:{agent}:{suffix}")
    bus.ROUTER_STREAM = "valentine:router:task"
    bus.add_task = AsyncMock()
    bus.publish = AsyncMock()
    bus.acknowledge_task = AsyncMock()
    return bus


@pytest.fixture
def make_task(sample_text_message):
    """Factory for creating AgentTask objects."""
    from valentine.models import AgentTask, RoutingDecision, AgentName

    def _make(agent: AgentName, intent: str = "chat", text: str = "Hello", **kwargs):
        msg = IncomingMessage(
            message_id="test-123",
            chat_id="chat-456",
            user_id="user-789",
            platform="telegram",
            content_type=ContentType.TEXT,
            text=text,
            **kwargs,
        )
        return AgentTask(
            task_id="task-001",
            agent=agent,
            routing=RoutingDecision(intent=intent, agent=agent),
            message=msg,
        )
    return _make
```

- [ ] **Step 2: Verify fixtures load**

Run: `pytest tests/test_models.py -v`
Expected: Existing tests still pass

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add mock LLM and bus fixtures for agent testing"
```

---

### Task 10: Agent unit tests

Test each agent's `process_task` with mocked LLM and bus.

**Files:**
- Create: `tests/test_oracle.py`
- Create: `tests/test_zeroclaw.py`
- Create: `tests/test_iris.py`
- Create: `tests/test_codesmith.py` (basic — no shell execution)

- [ ] **Step 1: Write Oracle tests**

```python
# tests/test_oracle.py
import pytest
from unittest.mock import AsyncMock
from valentine.agents.oracle import OracleAgent
from valentine.models import AgentName, AgentTask, RoutingDecision, IncomingMessage, ContentType


class TestOracleAgent:
    @pytest.mark.asyncio
    async def test_basic_chat(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="Hey there! How can I help?")
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ORACLE,
            routing=RoutingDecision(intent="chat", agent=AgentName.ORACLE),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="Hi Valentine",
            ),
        )
        result = await agent.process_task(task)
        assert result.success is True
        assert result.text == "Hey there! How can I help?"

    @pytest.mark.asyncio
    async def test_saves_to_history(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="Response")
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ORACLE,
            routing=RoutingDecision(intent="chat", agent=AgentName.ORACLE),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="Hello",
            ),
        )
        await agent.process_task(task)
        # Should have called append_history twice: once for user, once for assistant
        assert mock_bus.append_history.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(side_effect=Exception("API error"))
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ORACLE,
            routing=RoutingDecision(intent="chat", agent=AgentName.ORACLE),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="Hello",
            ),
        )
        result = await agent.process_task(task)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_reply_context_included(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="About that message...")
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ORACLE,
            routing=RoutingDecision(intent="chat", agent=AgentName.ORACLE),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="What about this?", reply_to_text="The weather is nice today",
            ),
        )
        result = await agent.process_task(task)
        # Verify the LLM was called with reply context in the prompt
        call_args = mock_llm.chat_completion.call_args
        messages = call_args[0][0]  # First positional arg
        user_msg = messages[-1]["content"]
        assert "weather is nice" in user_msg
```

- [ ] **Step 2: Write ZeroClaw tests**

```python
# tests/test_zeroclaw.py
import pytest
from unittest.mock import AsyncMock
from valentine.orchestrator.zeroclaw import ZeroClawRouter
from valentine.models import (
    AgentName, AgentTask, RoutingDecision, IncomingMessage, ContentType,
)


class TestZeroClawRouter:
    @pytest.mark.asyncio
    async def test_photo_always_routes_to_iris(self, mock_llm, mock_bus):
        """Photos should ALWAYS go to Iris, regardless of LLM response."""
        # LLM says "oracle" but content_type is photo — should override
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "describe photo", "agent": "oracle", "priority": "normal"}'
        )
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.PHOTO,
                text="What's in this image?", media_path="/tmp/photo.jpg",
            ),
        )
        result = await router.process_task(task)
        # Verify the task was routed to Iris
        add_task_calls = mock_bus.add_task.call_args_list
        routed_stream = add_task_calls[-1][0][0]  # First arg of last add_task call
        assert "iris" in routed_stream

    @pytest.mark.asyncio
    async def test_voice_always_routes_to_echo(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "transcribe", "agent": "oracle", "priority": "normal"}'
        )
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.VOICE,
                media_path="/tmp/voice.ogg",
            ),
        )
        result = await router.process_task(task)
        add_task_calls = mock_bus.add_task.call_args_list
        routed_stream = add_task_calls[-1][0][0]
        assert "echo" in routed_stream

    @pytest.mark.asyncio
    async def test_bad_json_falls_back_to_oracle(self, mock_llm, mock_bus):
        """If LLM returns garbage, should fall back to Oracle."""
        mock_llm.chat_completion = AsyncMock(return_value="This is not JSON at all lol")
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="Hello",
            ),
        )
        result = await router.process_task(task)
        add_task_calls = mock_bus.add_task.call_args_list
        routed_stream = add_task_calls[-1][0][0]
        assert "oracle" in routed_stream

    @pytest.mark.asyncio
    async def test_invalid_priority_doesnt_crash(self, mock_llm, mock_bus):
        """Invalid priority from LLM should not crash routing."""
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "chat", "agent": "oracle", "priority": "SUPER_URGENT"}'
        )
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        task = AgentTask(
            task_id="t1", agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=IncomingMessage(
                message_id="1", chat_id="c1", user_id="u1",
                platform="telegram", content_type=ContentType.TEXT,
                text="Hello",
            ),
        )
        result = await router.process_task(task)
        assert result.success is True  # Should not crash
```

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_oracle.py tests/test_zeroclaw.py
git commit -m "test: add Oracle and ZeroClaw agent unit tests"
```

---

### Task 11: Honest README update

Update the README to accurately reflect what's real and what's conditional.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update capabilities section**

Remove any claims about capabilities that aren't wired up. Be honest about what requires external dependencies:

- Mark vision as "requires Groq free tier API key"
- Mark memory as "requires Qdrant running"
- Mark browser as "requires Playwright installed"
- Mark voice as "requires ffmpeg installed"
- Remove any remaining "v2" references
- Update Nexus description from vague "tool integrations" to specific: "Weather (Open-Meteo), Crypto prices (CoinGecko)"

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README to honestly reflect current capabilities"
```

---

## Task Dependency Graph

```
Phase 1 (Safety):
  Task 1 (safe JSON) ──→ Task 4, 5, 6, 7 depend on it
  Task 2 (Cortex)    ──→ independent
  Task 3 (errors)    ──→ independent

Phase 2 (Functionality):
  Task 4 (Nexus)     ──→ depends on Task 1
  Task 5 (RAG)       ──→ depends on Task 1
  Task 6 (Evolution) ──→ independent
  Task 7 (DocGen)    ──→ depends on Task 1
  Task 8 (Senses)    ──→ independent

Phase 3 (Tests):
  Task 9 (fixtures)  ──→ must come before Task 10
  Task 10 (tests)    ──→ depends on Task 9, and all Phase 1+2 tasks
  Task 11 (README)   ──→ last
```

**Parallelizable groups:**
- Tasks 1, 2, 3 can all run in parallel
- Tasks 4, 5, 6, 7, 8 can run in parallel (after Task 1)
- Tasks 9 → 10 → 11 are sequential
