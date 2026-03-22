# src/valentine/agents/codesmith.py
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import List

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block
from valentine.models import AgentName, AgentTask, TaskResult
from valentine.config import settings
from valentine.utils import safe_parse_json

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

    @property
    def system_prompt(self) -> str:
        skills_list = self._discover_skills()

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

        return (
            identity_block()
            + "Currently operating in engineering mode. You're a world-class full-stack "
            "developer, DevOps engineer, and systems architect. You write clean, "
            "production-quality code and explain your thinking clearly.\n\n"
            "You have access to a sandboxed workspace where you can execute shell commands "
            "and manage files. When the user asks you to write code, run commands, debug, "
            "or build something, you use structured actions.\n\n"
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
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=settings.max_shell_timeout,
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
                evolver = SelfEvolver(allow_apt=False)
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
        messages.extend(history[:-1])  # history minus the message we just added

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
                messages, temperature=0.1, **kwargs,
            )

            actions = safe_parse_json(response_text)
            if actions is None:
                # If LLM didn't return JSON, treat entire response as text
                if chat_id:
                    await self.bus.append_history(chat_id, "assistant", response_text)
                return TaskResult(
                    task_id=task.task_id, agent=self.name,
                    success=True, text=response_text,
                )

            if isinstance(actions, dict) and "actions" in actions:
                actions = actions["actions"]
            elif isinstance(actions, dict):
                actions = [actions]

            execution_log = []
            final_response = ""

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
                elif act == "respond":
                    final_response = action.get("text", "")

            # If LLM didn't include a respond action, summarize what happened
            if not final_response:
                final_response = "Done! Here's what I executed:"

            if execution_log:
                out_txt = final_response + "\n\n" + "\n\n".join(execution_log)
            else:
                out_txt = final_response

            # Save assistant response to history
            if chat_id:
                await self.bus.append_history(chat_id, "assistant", out_txt[:500])

            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, text=out_txt[:4000],
            )

        except Exception as e:
            logger.exception("CodeSmith processing failed")
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error=str(e),
            )
