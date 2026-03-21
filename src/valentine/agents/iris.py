# src/valentine/agents/iris.py
import logging
import urllib.parse
from typing import Dict, Any

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.llm import MultimodalProvider

logger = logging.getLogger(__name__)

class IrisAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.IRIS,
            llm=llm,
            bus=bus,
            consumer_group="iris_workers",
            consumer_name="iris_1"
        )
        self.multimodal_llm = llm if isinstance(llm, MultimodalProvider) else None

    @property
    def system_prompt(self) -> str:
        return """You are Iris, the precision vision analyst and image generation agent for Valentine v2.
If the user provides an image or asks a question about an image, analyze it closely.
If the user asks to generate an image, respond ONLY with a JSON object:
{"action": "generate", "prompt": "highly detailed descriptive image generation prompt"}
For 'screenshot-to-code', describe the UI components in extreme technical detail so CodeSmith can construct it perfectly."""

    def _generate_image_url(self, prompt: str) -> str:
        encoded_prompt = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"

    async def process_task(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        msg = task.message
        
        has_image = msg.media_path is not None and msg.content_type == ContentType.PHOTO
        
        try:
            # Handle image generation intent
            if intent == "generate_image" or "generate an image" in (msg.text or "").lower():
                messages = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": f"Formulate a highly detailed image generation prompt for this request: {msg.text}"}
                ]
                kwargs = {}
                if self.llm.provider_name in ["groq", "cerebras"]:
                    kwargs["response_format"] = {"type": "json_object"}
                    
                response_text = await self.llm.chat_completion(messages, temperature=0.5, **kwargs)
                import json
                try:
                    data = json.loads(response_text.replace("```json", "").replace("```", "").strip())
                    gen_prompt = data.get("prompt", msg.text)
                except Exception:
                    gen_prompt = msg.text
                    
                image_url = self._generate_image_url(gen_prompt or "beautiful abstract image")
                return TaskResult(
                    task_id=task.task_id,
                    agent=self.name,
                    success=True,
                    content_type=ContentType.PHOTO,
                    text=f"Generated image for: {gen_prompt}",
                    media_path=image_url
                )

            # Handle Vision analysis
            if has_image:
                if not self.multimodal_llm:
                    return TaskResult(task_id=task.task_id, agent=self.name, success=False, error="SambaNova multimodal provider not configured.")
                
                target_prompt = msg.text if msg.text else "Please describe this image in detail."
                if intent == "ocr":
                    target_prompt = "Extract all text from this image exactly as written. " + target_prompt
                elif intent == "screenshot_to_code":
                    target_prompt = "Describe this UI in extreme detail structurally and stylistically so a frontend developer can build it flawlessly. " + target_prompt
                
                analysis = await self.multimodal_llm.image_completion(target_prompt, msg.media_path)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=analysis)
                
            # Default fallback for simple chat to Iris
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": msg.text or ""}
            ]
            text_response = await self.llm.chat_completion(messages)
            return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=text_response)
            
        except Exception as e:
            logger.exception("Iris logic failed")
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error=str(e))
