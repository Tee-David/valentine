# src/valentine/agents/codesmith.py
import logging
import subprocess
import os
import json
from typing import List, Dict, Any

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult
from valentine.config import settings

logger = logging.getLogger(__name__)

class CodeSmithAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.CODESMITH,
            llm=llm,
            bus=bus,
            consumer_group="codesmith_workers",
            consumer_name="codesmith_1"
        )
        self.workspace = settings.workspace_dir
        os.makedirs(self.workspace, exist_ok=True)
        self.denylist = ["rm -rf /", "mkfs", "dd", "shutdown", "reboot"]

    @property
    def system_prompt(self) -> str:
        return """You are CodeSmith, the senior full-stack engineer and DevOps agent for Valentine v2.
You have access to sandboxed shell execution and file system operations within the workspace.
When answering, provide a JSON array of actions you wish to take.
Valid actions:
{"action": "shell", "command": "npm init -y"}
{"action": "write", "path": "index.js", "content": "console.log('hello');"}
{"action": "read", "path": "package.json"}
{"action": "respond", "text": "Final response to user"}

Output ONLY a JSON array, for example:
[
  {"action": "shell", "command": "echo 'Starting'"},
  {"action": "respond", "text": "Done."}
]
"""

    def _is_safe(self, command: str) -> bool:
        for bad in self.denylist:
            if bad in command:
                return False
        return True

    def _execute_shell(self, command: str) -> str:
        if not self._is_safe(command):
            return "Error: Command is in the denylist and blocked for security."
        try:
            result = subprocess.run(
                command, 
                shell=True, 
                cwd=self.workspace, 
                capture_output=True, 
                text=True, 
                timeout=settings.max_shell_timeout
            )
            out = result.stdout.strip()
            err = result.stderr.strip()
            if result.returncode == 0:
                return out if out else "[Command succeeded with no output]"
            return f"[Error code {result.returncode}]: {err}"
        except subprocess.TimeoutExpired:
            return "Error: Command execution timed out."
        except Exception as e:
            return f"Error executing command: {e}"

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
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return "File successfully written."
        except Exception as e:
            return f"Error writing file: {e}"

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        context_str = "\n".join(task.routing.memory_context) if task.routing.memory_context else ""
        
        prompt = f"User Request: {msg.text}\n"
        if context_str:
            prompt += f"Context:\n{context_str}\n"
        
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        try:
            kwargs = {}
            if self.llm.provider_name in ["groq", "cerebras"]:
                kwargs["response_format"] = {"type": "json_object"}
                
            response_text = await self.llm.chat_completion(messages, temperature=0.1, **kwargs)
            
            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            
            try:
                actions = json.loads(clean_text)
            except json.JSONDecodeError:
                return TaskResult(task_id=task.task_id, agent=self.name, success=False, error="Invalid JSON output from LLM.")
                
            if isinstance(actions, dict) and "actions" in actions:
                actions = actions["actions"]
            elif isinstance(actions, dict):
                actions = [actions]
                
            execution_log = []
            final_response = "Operations processed."
            
            for action in actions:
                act = action.get("action")
                if act == "shell":
                    cmd = action.get("command", "")
                    res = self._execute_shell(cmd)
                    execution_log.append(f"$ {cmd}\n{res}")
                elif act == "read":
                    path = action.get("path", "")
                    res = self._read_file(path)
                    execution_log.append(f"read {path}:\n{res}")
                elif act == "write":
                    path = action.get("path", "")
                    content = action.get("content", "")
                    res = self._write_file(path, content)
                    execution_log.append(f"write {path}: {res}")
                elif act == "respond":
                    final_response = action.get("text", final_response)
                    
            if execution_log:
                out_txt = final_response + "\n\nExecution Log:\n" + "\n".join(execution_log)
            else:
                out_txt = final_response
                
            return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=out_txt[:4000])
            
        except Exception as e:
            logger.exception("CodeSmith logic failed")
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error=str(e))
