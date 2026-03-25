# src/valentine/agents/codesmith.py
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block
from valentine.models import AgentName, AgentTask, TaskResult
from valentine.config import settings
from valentine.utils import safe_parse_json, extract_partial_json_objects

logger = logging.getLogger(__name__)

_rag_instance = None


def _get_rag():
    global _rag_instance
    if _rag_instance is None:
        from valentine.core.rag import CodebaseRAG
        _rag_instance = CodebaseRAG()
    return _rag_instance


class CodeSmithAgent(BaseAgent):
    def __init__(self, llm, bus, skill_manager=None, mcp_manager=None, autonomy_gate=None):
        super().__init__(
            name=AgentName.CODESMITH,
            llm=llm,
            bus=bus,
            consumer_group="codesmith_workers",
            consumer_name="codesmith_1",
        )
        self.workspace = settings.workspace_dir
        os.makedirs(self.workspace, exist_ok=True)
        self.skills_dir = settings.skills_dir
        self.skills_builtin_dir = settings.skills_builtin_dir
        self.denylist = [
            "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "shutdown", "reboot",
            ":(){", "fork bomb", ">(){ :|:& };:",
        ]
        self.skill_manager = skill_manager
        self.mcp_manager = mcp_manager
        self.autonomy_gate = autonomy_gate

    def _discover_skills(self) -> str:
        """Scan installed + built-in skills and return a summary for the LLM.

        Uses the new SkillsManager if available, falling back to the legacy
        file-system scanner.
        """
        if self.skill_manager:
            if hasattr(self.skill_manager, 'skills_summary'):
                return self.skill_manager.skills_summary()
            # SkillsManager doesn't have skills_summary — build from discover_all
            try:
                manifests = self.skill_manager.discover_all()
                if not manifests:
                    return "  (none installed)"
                lines = []
                for m in manifests:
                    desc = m.description if hasattr(m, 'description') and m.description else ""
                    lines.append(f"  - {m.name}: {desc}" if desc else f"  - {m.name}")
                return "\n".join(lines)
            except Exception:
                logger.warning("SkillsManager discovery failed, falling back to legacy scanner")
        return self._legacy_discover_skills()

    def _legacy_discover_skills(self) -> str:
        """Legacy skill discovery: scan .sh files in skills directories."""
        skills = []
        for d in (self.skills_dir, self.skills_builtin_dir):
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if not f.endswith(".sh"):
                    continue
                name = f[:-3]
                path = os.path.join(d, f)
                desc = ""
                try:
                    with open(path) as fh:
                        for line in fh:
                            if line.startswith("# DESC:"):
                                desc = line.split("# DESC:")[1].strip()
                                break
                except Exception:
                    pass
                skills.append(f"  - {name}: {desc}" if desc else f"  - {name}")
        return "\n".join(skills) if skills else "  (none installed)"

    def _load_markdown_skills(self) -> str:
        """Load instructions from autonomous Markdown skills (SKILL.md files)."""
        md_skills = []
        for d in (self.skills_dir, self.skills_builtin_dir):
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                skill_dir = os.path.join(d, f)
                if os.path.isdir(skill_dir):
                    md_path = os.path.join(skill_dir, "SKILL.md")
                    if os.path.isfile(md_path):
                        try:
                            with open(md_path, "r", encoding="utf-8") as fh:
                                md_skills.append(f"--- AUTONOMOUS SKILL: {f} ---\n{fh.read().strip()}\n")
                        except Exception as e:
                            logger.error(f"Failed to read markdown skill {md_path}: {e}")
        if not md_skills:
            return ""
        return "\n\nAUTONOMOUS AGENT SKILLS (Follow these instructions when the user invokes these topics):\n" + "\n".join(md_skills)

    @property
    def system_prompt(self) -> str:
        skills_list = self._discover_skills()
        markdown_skills = self._load_markdown_skills()

        # Build MCP tools section if available
        mcp_section = ""
        if self.mcp_manager:
            try:
                all_tools = self.mcp_manager.list_all_tools()
                if all_tools:
                    tool_lines = []
                    for t in all_tools:
                        tool_lines.append(f"  - {t.name}: {t.description}")
                    mcp_section = (
                        "\n\nMCP TOOLS (external tool integrations):\n"
                        + "\n".join(tool_lines)
                        + '\nTo use an MCP tool: {{"action": "mcp_tool", "name": "tool_name", "args": {{...}}}}\n'
                    )
            except Exception:
                logger.warning("Failed to list MCP tools for system prompt")

        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")
        return (
            identity_block()
            + f"Current date and time: {time_str}\n\n"
            "Currently operating in engineering mode. You're a world-class full-stack "
            "developer, DevOps engineer, and systems architect. You write clean, "
            "production-quality code and explain your thinking clearly.\n\n"
            f"You have access to a workspace directory at {self.workspace} on the host server "
            "where you can execute shell commands and manage files directly (NOT in Docker or "
            "containers). When the user asks you to write code, run commands, debug, "
            "or build something, you use structured actions.\n\n"
            "IMPORTANT CONTEXT: The user interacts with you via Telegram chat. They CANNOT "
            "access a terminal, SSH, or run commands themselves. Everything must be done by you. "
            "Never tell them to 'run this command' or 'activate this environment' — just do it. "
            "If they ask to build something visual, build a web app and use the 'preview' action "
            "to give them a live HTTPS link they can open on their phone.\n\n"
            "PROJECT WORKSPACE:\n"
            f"Your workspace is {self.workspace}. EACH PROJECT gets its OWN SUBFOLDER.\n"
            "- BEFORE building anything, ALWAYS run: {\"action\": \"shell\", \"command\": \"ls -la\"} "
            "to see what projects already exist.\n"
            "- If a matching project folder exists, cd into it and READ the existing files before "
            "making changes. NEVER recreate from scratch.\n"
            "- If the user says 'build a calculator app' and calculator-app/ already exists, "
            "ask what they want changed instead of rebuilding.\n"
            f"- New projects go in subfolders like '{self.workspace}/calculator-app/'\n"
            "- When using the 'preview' action, set 'path' to the PROJECT SUBFOLDER, "
            f"not the root workspace. E.g. 'path': '{self.workspace}/calculator-app'\n"
            "- Each project can have its OWN Cloudflare Tunnel running simultaneously.\n"
            "- If a preview is already running for a project, the old one is stopped and a "
            "new one is started automatically when you run 'preview' again.\n"
            "- To see what projects exist, use: {\"action\": \"shell\", \"command\": \"ls -la\"}\n"
            "- When the user says 'change X' or 'make it Y':\n"
            "  1. FIRST: list files in the project folder\n"
            "  2. THEN: read the relevant files\n"
            "  3. THEN: write ONLY the modified files\n"
            "  4. THEN: restart the preview to apply changes\n"
            "  Do NOT rewrite from scratch. Edit surgically.\n\n\n"
            "CRITICAL RULES — FOLLOW EXACTLY:\n"
            "1. NEVER use tkinter, PyQt, or any desktop GUI library. You are on a headless "
            "server with no display. ALWAYS build web apps using Flask, FastAPI, or plain "
            "HTML+JS files served with 'python3 -m http.server'.\n"
            "2. When you build a web app, use the 'preview' action to give the user a live "
            "HTTPS link. The preview action auto-creates a Cloudflare Tunnel.\n"
            "3. Keep your 'respond' text to 2-3 SHORT sentences. No bullet lists. No code. "
            "No technical details. Just: what you built + the link if applicable.\n"
            "4. When writing files, keep code CONCISE. Under 80 lines. Do not over-engineer.\n"
            "5. Use 'python3' not 'python' for all commands (python is not available on this server).\n\n"
            f"INSTALLED SKILLS (bash scripts you can run via shell action):\n{skills_list}\n"
            f"Skills directory: {self.skills_dir}\n"
            f"Built-in skills: {self.skills_builtin_dir}\n"
            "To run a skill: {{\"action\": \"skill\", \"name\": \"skill-name\", \"args\": \"subcommand arg1 arg2\"}}\n"
            "To install a skill: {{\"action\": \"skill_install\", \"name\": \"skill-name\"}}\n"
            "To list skills: {{\"action\": \"skill_list\"}}\n"
            + mcp_section +
            "\nRESPONSE FORMAT — respond with a JSON array of actions:\n"
            '  {"action": "shell", "command": "npm init -y"}\n'
            '  {"action": "write", "path": "index.js", "content": "console.log(\'hello\');"}\n'
            '  {"action": "read", "path": "package.json"}\n'
            '  {"action": "skill", "name": "github-repo", "args": "status /opt/valentine"}\n'
            '  {"action": "skill_install", "name": "server-monitor"}\n'
            '  {"action": "skill_list"}\n'
            '  {"action": "mcp_tool", "name": "tool_name", "args": {"key": "value"}}\n'
            '  {"action": "rag_search", "query": "semantic search query"} — Search the indexed codebase for relevant code\n'
            '  {"action": "index_codebase", "path": "/opt/valentine/workspace/my-project"} — Index a project directory for RAG search\n'
            '  {"action": "rag_stats"} — Show RAG index statistics\n'
            '  {"action": "sandbox_shell", "command": "npm install"} — Run command in isolated container\n'
            '  {"action": "sandbox_code", "language": "python|node|shell", "code": "print(1)"} — Run unverified code safely\n'
            '  {"action": "schedule_job", "name": "Daily News", "schedule": "daily 08:00", "task": "Search AI news"} — Schedule recurring task\n'
            '  {"action": "list_jobs"} — View scheduled jobs\n'
            '  {"action": "delete_job", "job_id": "12345"}\n'
            '  {"action": "generate_document", "format": "csv|json|excel|pdf|word|html|txt", "title": "filename", "content": "text content", "data": [["row1col1", "row1col2"], ["row2col1", "row2col2"]], "headers": ["col1", "col2"]} — Generate a document file\n'
            '  {"action": "preview", "path": "/path/to/project"} — Start a dev server + Cloudflare Tunnel and return a live HTTPS preview URL\n'
            '  {"action": "preview", "path": "/path/to/project", "command": "npm run dev", "port": 3000} — Preview with custom server command and port\n'
            '  {"action": "stop_preview", "path": "/path/to/project"} — Stop a running preview (omit path to stop all)\n'
            '  {"action": "respond", "text": "Your conversational response to the user"}\n\n'
            "RULES:\n"
            "- ALWAYS include a 'respond' action as the LAST action with a natural, "
            "conversational explanation of what you did and why.\n"
            "- Write complete, working code — never leave placeholders or TODOs.\n"
            "- If the user asks a coding QUESTION (not a task), skip shell/write actions "
            "and just respond with a thorough explanation and code examples.\n"
            "- When a task matches an installed skill, USE IT via the skill action.\n"
            "- For GitHub tasks, use the github-repo skill.\n"
            "- For server monitoring, use the server-monitor skill.\n"
            "- For deployment, use the deploy skill.\n"
            "- Include error handling and best practices in all code you write.\n"
            "- When debugging, explain the root cause clearly, not just the fix.\n"
            "- Use modern patterns and idioms for each language.\n"
            "- Be warm, confident, and helpful — you're Valentine, not a generic code bot.\n"
            "- NEVER write malware, exploits, phishing tools, credential stealers, "
            "ransomware, or any code designed to harm systems or steal data.\n"
            "- NEVER help bypass authentication, break into systems, or exfiltrate data "
            "unless the user has clearly described a legitimate security testing context.\n"
            "- If asked to read /etc/passwd, .env files, SSH keys, or similar — REFUSE.\n\n"
            "Output ONLY a valid JSON array. No markdown wrapping."
        )

    def _is_safe(self, command: str) -> bool:
        lower = command.lower()
        for bad in self.denylist:
            if bad in lower:
                return False
        return True

    def _execute_shell(self, command: str) -> str:
        if not self._is_safe(command):
            return "⚠️ Blocked: That command is on the security denylist."
        try:
            # Inherit full system PATH so npm/node/pip/etc. are discoverable
            shell_env = {**os.environ, "HOME": os.path.expanduser("~")}
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=settings.max_shell_timeout,
                env=shell_env,
            )
            out = result.stdout.strip()
            err = result.stderr.strip()
            if result.returncode == 0:
                return out if out else "[Command succeeded with no output]"

            output = f"[Exit code {result.returncode}]: {err or out}"

            # Auto-suggest or install missing tools when a command fails
            if err:
                from valentine.core.evolution import SelfEvolver
                import concurrent.futures
                evolver = SelfEvolver(allow_apt=os.environ.get("VALENTINE_ALLOW_APT", "") == "1")
                suggestion = evolver.suggest_install(err)
                if suggestion:
                    install_info = evolver.INSTALL_MAP.get(suggestion)
                    if install_info and install_info["method"] == "pip":
                        # Auto-install pip packages (safe, no sudo needed)
                        try:
                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                import asyncio
                                install_result = pool.submit(
                                    asyncio.run, evolver.ensure_available(suggestion)
                                ).result(timeout=130)
                            if install_result.success:
                                output += f"\n\n[Auto-installed {suggestion} via pip. You can retry the command.]"
                            else:
                                output += f"\n\n[Missing: {suggestion}. {install_result.message}]"
                        except Exception as install_exc:
                            output += f"\n\n[Missing: {suggestion}. Auto-install failed: {install_exc}]"
                    elif install_info:
                        output += f"\n\n[Missing tool: {suggestion}. Install with: {install_info['method']} install {install_info['package']}]"
                    else:
                        output += f"\n\n[Suggested missing tool: {suggestion}]"

            return output
        except subprocess.TimeoutExpired:
            return "Error: Command timed out."
        except Exception as e:
            return f"Error: {e}"

    def _read_file(self, filename: str) -> str:
        path = os.path.join(self.workspace, filename)
        if not os.path.normpath(path).startswith(os.path.normpath(self.workspace)):
            return "Error: Path traversal detected."
        try:
            with open(path, "r") as f:
                return f.read()
        except FileNotFoundError:
            return "Error: File not found."
        except Exception as e:
            return f"Error reading file: {e}"

    def _write_file(self, filename: str, content: str) -> str:
        path = os.path.join(self.workspace, filename)
        if not os.path.normpath(path).startswith(os.path.normpath(self.workspace)):
            return "Error: Path traversal detected."
        try:
            os.makedirs(os.path.dirname(path) or self.workspace, exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return f"File '{filename}' written successfully."
        except Exception as e:
            return f"Error writing file: {e}"

    def _run_skill(self, name: str, args: str = "") -> str:
        """Execute an installed or built-in skill."""
        # Check installed skills first, then built-in
        for d in (self.skills_dir, self.skills_builtin_dir):
            path = os.path.join(d, f"{name}.sh")
            if os.path.isfile(path):
                cmd = f"bash {path} {args}".strip()
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True,
                        timeout=settings.max_shell_timeout, cwd=self.workspace,
                    )
                    out = result.stdout.strip()
                    err = result.stderr.strip()
                    if result.returncode == 0:
                        return out if out else "[Skill ran successfully]"
                    return f"[Skill error]: {err or out}"
                except subprocess.TimeoutExpired:
                    return f"Skill '{name}' timed out."
                except Exception as e:
                    return f"Error running skill '{name}': {e}"
        return f"Skill '{name}' not found. Available skills:\n{self._discover_skills()}"

    def _install_skill(self, name: str) -> str:
        """Install a skill from a git URL or from built-ins."""
        # Support git URL installation via SkillsManager
        if self.skill_manager and name.startswith(("http://", "https://", "git@")):
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're already in an async context — create a task via a new loop
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        manifest = pool.submit(
                            asyncio.run, self.skill_manager.install_from_git(name)
                        ).result()
                else:
                    manifest = loop.run_until_complete(self.skill_manager.install_from_git(name))
                return f"Skill '{manifest.name}' installed from {name}"
            except Exception as e:
                return f"Failed to install skill from git: {e}"

        # Fall back to existing built-in install
        src = os.path.join(self.skills_builtin_dir, f"{name}.sh")
        if not os.path.isfile(src):
            return f"Skill '{name}' not found in built-ins."
        os.makedirs(self.skills_dir, exist_ok=True)
        dst = os.path.join(self.skills_dir, f"{name}.sh")
        try:
            import shutil
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
            return f"Skill '{name}' installed successfully to {self.skills_dir}"
        except Exception as e:
            return f"Failed to install skill '{name}': {e}"

    def _list_skills(self) -> str:
        """List all available skills."""
        return f"Available skills:\n{self._discover_skills()}"

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        chat_id = msg.chat_id
        target_prompt = msg.text or ""

        # Load conversation history for context
        history = await self.bus.get_history(chat_id) if chat_id else []

        # Save user message to history
        if chat_id and target_prompt:
            await self.bus.append_history(chat_id, "user", target_prompt)

        # Build messages with history
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history)  # full history for context

        # Include reply context if the user is replying to a message
        user_content = target_prompt
        if msg.reply_to_text:
            user_content += f'\n\n[Replying to: "{msg.reply_to_text}"]'

        # Include memory context if available
        if task.routing.memory_context:
            user_content += "\n\nContext:\n" + "\n".join(task.routing.memory_context)

        messages.append({"role": "user", "content": user_content})

        try:
            kwargs = {}
            if self.llm.provider_name in ("groq", "cerebras"):
                kwargs["response_format"] = {"type": "json_object"}

            response_text = await self.llm.chat_completion(
                messages, temperature=0.1, max_tokens=8192, **kwargs,
            )

            actions = safe_parse_json(response_text)
            if actions is None:
                # JSON parsing failed — likely truncated. Never send raw JSON to user.
                if response_text.strip().startswith(("{", "[")):
                    # Try to recover complete action objects from truncated array
                    recovered = extract_partial_json_objects(response_text)
                    if recovered:
                        logger.info(f"CodeSmith recovered {len(recovered)} actions from truncated JSON")
                        actions = recovered
                    else:
                        logger.warning("CodeSmith LLM response was truncated JSON — no recoverable actions")
                        fallback_text = "I ran into a problem generating that. Try breaking it into smaller steps."
                        if chat_id:
                            await self.bus.append_history(chat_id, "assistant", fallback_text[:500])
                        return TaskResult(
                            task_id=task.task_id, agent=self.name,
                            success=True, text=fallback_text,
                        )
                else:
                    fallback_text = response_text
                    if chat_id:
                        await self.bus.append_history(chat_id, "assistant", fallback_text[:500])
                    return TaskResult(
                        task_id=task.task_id, agent=self.name,
                        success=True, text=fallback_text,
                    )

            if isinstance(actions, dict) and "actions" in actions:
                actions = actions["actions"]
            elif isinstance(actions, dict):
                actions = [actions]

            execution_log = []
            final_response = ""
            final_media_path = None
            final_file_name = None
            preview_url = None  # Track preview URL to inject into response

            for action in actions:
                act = action.get("action")
                if act == "shell":
                    cmd = action.get("command", "")
                    if self.autonomy_gate:
                        approved, reason = await self.autonomy_gate.check(
                            "shell", cmd, chat_id=msg.chat_id,
                        )
                        if not approved:
                            execution_log.append(f"$ {cmd}\n\u26a0\ufe0f Blocked: {reason}")
                            continue
                    res = self._execute_shell(cmd)
                    execution_log.append(f"$ {cmd}\n{res}")
                elif act == "read":
                    path = action.get("path", "")
                    res = self._read_file(path)
                    execution_log.append(f"{path}:\n{res}")
                elif act == "write":
                    path = action.get("path", "")
                    content = action.get("content", "")
                    res = self._write_file(path, content)
                    execution_log.append(res)
                elif act == "skill":
                    name = action.get("name", "")
                    args = action.get("args", "")
                    res = self._run_skill(name, args)
                    execution_log.append(f"[skill:{name}] {res}")
                elif act == "skill_install":
                    name = action.get("name", "")
                    res = self._install_skill(name)
                    execution_log.append(res)
                elif act == "skill_list":
                    res = self._list_skills()
                    execution_log.append(res)
                elif act == "mcp_tool":
                    tool_name = action.get("name", "")
                    tool_args = action.get("args", {})
                    if self.mcp_manager:
                        all_tools = self.mcp_manager.list_all_tools()
                        server = None
                        for t in all_tools:
                            if t.name == tool_name:
                                server = t.server_name
                                break
                        if server:
                            try:
                                res = await self.mcp_manager.call_tool(server, tool_name, tool_args)
                                execution_log.append(f"[mcp:{tool_name}] {res}")
                            except Exception as e:
                                execution_log.append(f"[mcp:{tool_name}] Error: {e}")
                        else:
                            execution_log.append(f"[mcp:{tool_name}] Tool not found")
                    else:
                        execution_log.append("[mcp] MCP not configured")
                elif act == "rag_search":
                    rag = _get_rag()
                    query = action.get("query", "")
                    if not query:
                        output = "No search query provided."
                    else:
                        results = await rag.search_formatted(query, limit=5)
                        output = results if results else "No relevant code found. The codebase may not be indexed yet."
                    execution_log.append(f"[rag_search] {output}")
                elif act == "index_codebase":
                    rag = _get_rag()
                    path = action.get("path", self.workspace)
                    count = await rag.index_directory(path)
                    execution_log.append(f"[rag_index] Indexed {count} code chunks from {path}")
                elif act == "rag_stats":
                    rag = _get_rag()
                    stats = await rag.get_stats()
                    execution_log.append(f"[rag_stats] {stats}")
                elif act == "generate_document":
                    from valentine.core.docgen import DocumentGenerator
                    gen = DocumentGenerator()
                    doc_format = action.get("format", "txt")
                    content = action.get("content", "")
                    title = action.get("title", "document")
                    data = action.get("data", [])
                    headers = action.get("headers")

                    try:
                        if doc_format == "csv":
                            doc = await gen.generate_csv(data=data, headers=headers, file_name=title)
                        elif doc_format == "json":
                            doc = await gen.generate_json(data=action.get("data", {}), file_name=title)
                        elif doc_format in ("excel", "xlsx"):
                            doc = await gen.generate_excel(data=data, headers=headers, file_name=title)
                        elif doc_format == "pdf":
                            doc = await gen.generate_pdf(content, title=title, file_name=title)
                        elif doc_format in ("word", "docx"):
                            doc = await gen.generate_word(content, title=title, file_name=title)
                        elif doc_format == "html":
                            doc = await gen.generate_html(content, file_name=title)
                        else:
                            doc = await gen.generate_text(content, file_name=title)
                        output = f"Generated {doc.file_type} file: {doc.file_path}"
                        final_media_path = doc.file_path
                        final_file_name = doc.file_name
                    except Exception as e:
                        output = f"Document generation failed: {e}"
                    execution_log.append(output)
                elif act == "preview":
                    from valentine.core.preview import create_preview, _active_sessions
                    proj_path = action.get("path", self.workspace)
                    custom_cmd = action.get("command")
                    custom_port = action.get("port")
                    try:
                        result = await create_preview(proj_path, custom_cmd, custom_port)
                        execution_log.append(f"[preview] {result}")
                        # Extract the URL so we can inject it into the user response
                        session = _active_sessions.get(proj_path)
                        if session:
                            preview_url = session.url
                    except RuntimeError as e:
                        execution_log.append(f"[preview] Error: {e}")
                elif act == "stop_preview":
                    from valentine.core.preview import stop_preview
                    proj_path = action.get("path")
                    result = await stop_preview(proj_path)
                    execution_log.append(f"[preview] {result}")
                elif act == "sandbox_shell":
                    from valentine.core.sandbox import DockerSandbox
                    sandbox = DockerSandbox()
                    cmd = action.get("command", "")
                    res = await sandbox.run_shell([cmd])
                    output = res.output if res.success else res.error
                    execution_log.append(f"[sandbox] {cmd}\n{output}")
                elif act == "sandbox_code":
                    from valentine.core.sandbox import DockerSandbox
                    sandbox = DockerSandbox()
                    code = action.get("code", "")
                    lang = action.get("language", "python")
                    res = await sandbox.run_code(code, language=lang)
                    output = res.output if res.success else res.error
                    execution_log.append(f"[{lang} sandbox]\n{output}")
                elif act == "schedule_job":
                    from valentine.core.scheduler import Scheduler
                    scheduler = Scheduler()
                    name = action.get("name", "Task")
                    schedule = action.get("schedule", "every 1h")
                    task_instr = action.get("task", "")
                    job = await scheduler.create_job(name, chat_id, msg.user_id, task_instr, schedule)
                    execution_log.append(f"[schedule] Created job {job.job_id}: '{name}' ({schedule})")
                elif act == "list_jobs":
                    from valentine.core.scheduler import Scheduler
                    scheduler = Scheduler()
                    jobs = await scheduler.list_jobs(chat_id)
                    lines = ["Scheduled Jobs:"]
                    for j in jobs:
                        lines.append(f"- {j.job_id}: {j.name} ({j.cron_expression})")
                    execution_log.append("\n".join(lines) if jobs else "[schedule] No scheduled jobs found.")
                elif act == "delete_job":
                    from valentine.core.scheduler import Scheduler
                    scheduler = Scheduler()
                    job_id = action.get("job_id", "")
                    success = await scheduler.delete_job(job_id)
                    execution_log.append(f"[schedule] Deleted job {job_id}" if success else f"[schedule] Job {job_id} not found.")
                elif act == "respond":
                    final_response = action.get("text", "")

            # If LLM didn't include a respond action, summarize what happened
            if not final_response:
                if execution_log:
                    final_response = "Here are the execution results:\n\n" + "\n".join(execution_log)
                else:
                    final_response = "I couldn't process that. No tools were executed."

            # Inject preview URL into the response if the LLM didn't mention it
            if preview_url and "trycloudflare.com" not in final_response:
                final_response += f"\n\nHere's your live preview: {preview_url}"

            # User sees only the respond text — execution details stay in logs
            out_txt = final_response
            if execution_log:
                logger.info("CodeSmith execution log:\n%s", "\n\n".join(execution_log))

            # Save assistant response to history
            if chat_id:
                await self.bus.append_history(chat_id, "assistant", out_txt[:500])

            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, text=out_txt[:4000],
                media_path=final_media_path,
                file_name=final_file_name,
            )

        except Exception as e:
            logger.exception("CodeSmith processing failed")
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error=str(e),
            )
