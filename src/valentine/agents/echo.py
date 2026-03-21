# src/valentine/agents/echo.py
import json
import logging
import os
import subprocess
import uuid

from valentine.agents.base import BaseAgent
from valentine.models import (
    AgentName, AgentTask, TaskResult, ContentType,
    IncomingMessage, RoutingDecision,
)
from valentine.llm import AudioProvider
from valentine.config import settings

logger = logging.getLogger(__name__)


class EchoAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.ECHO,
            llm=llm,
            bus=bus,
            consumer_group="echo_workers",
            consumer_name="echo_1",
        )
        self.audio_llm = llm if isinstance(llm, AudioProvider) else None

    @property
    def system_prompt(self) -> str:
        return (
            "You are Echo, the voice and audio specialist for Valentine v2.\n"
            "You handle transcribed voice messages from the user and respond "
            "conversationally, concisely, and naturally as if speaking aloud.\n"
            "Do not use markdown formatting like asterisks or bold text, "
            "because your output will be spoken via TTS."
        )

    # ------------------------------------------------------------------
    # Audio helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_ogg_to_wav(ogg_path: str) -> str:
        """Convert Telegram OGG/Opus voice note to WAV using ffmpeg.

        Returns the path to the converted WAV file, or the original path
        if conversion fails (the transcription API may still accept it).
        """
        wav_path = os.path.splitext(ogg_path)[0] + ".wav"
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", ogg_path,
                "-ar", "16000",       # 16 kHz — optimal for Whisper
                "-ac", "1",           # mono
                "-c:a", "pcm_s16le",  # 16-bit PCM
                wav_path,
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0 and os.path.exists(wav_path):
                logger.info(f"Converted {ogg_path} → {wav_path}")
                return wav_path
            logger.warning(f"ffmpeg conversion failed: {proc.stderr}")
        except FileNotFoundError:
            logger.warning("ffmpeg not found — skipping OGG→WAV conversion")
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg conversion timed out")
        except Exception as e:
            logger.error(f"OGG→WAV conversion error: {e}")
        return ogg_path  # fall back to original file

    async def _generate_tts(self, text: str) -> str:
        """Generate audio response via local edge-tts."""
        out_path = os.path.join(
            settings.workspace_dir, f"response_{uuid.uuid4().hex[:8]}.mp3",
        )
        try:
            cmd = ["edge-tts", "--text", text, "--write-media", out_path]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and os.path.exists(out_path):
                return out_path
            logger.error(f"TTS generation failed: {proc.stderr}")
            return ""
        except FileNotFoundError:
            logger.warning("edge-tts not installed or not in PATH.")
            return ""
        except subprocess.TimeoutExpired:
            logger.warning("TTS generation timed out")
            return ""
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return ""

    # ------------------------------------------------------------------
    # Re-routing: send transcribed text back through ZeroClaw
    # ------------------------------------------------------------------

    async def _reroute_transcript(self, task: AgentTask, transcript: str):
        """Push the transcribed text back into ZeroClaw for intent routing.

        This lets a voice message like "search for the latest Python release"
        get routed to Oracle instead of Echo answering it directly.
        """
        original = task.message
        rerouted_msg = IncomingMessage(
            message_id=original.message_id,
            chat_id=original.chat_id,
            user_id=original.user_id,
            platform=original.platform,
            content_type=ContentType.TEXT,
            text=transcript,
            timestamp=original.timestamp,
        )
        rerouted_task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="voice_reroute", agent=AgentName.ZEROCLAW),
            message=rerouted_msg,
        )
        await self.bus.add_task(
            self.bus.ROUTER_STREAM, rerouted_task.to_dict(),
        )
        logger.info(
            f"Echo re-routed transcript for message {original.message_id} "
            f"back to ZeroClaw"
        )

    # ------------------------------------------------------------------
    # Main processing
    # ------------------------------------------------------------------

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message

        has_audio = (
            msg.media_path is not None and msg.content_type == ContentType.VOICE
        )
        transcript = msg.text or ""

        try:
            # 1. Transcription
            if has_audio and self.audio_llm:
                audio_path = msg.media_path

                # Convert OGG → WAV if needed (Telegram sends OGG/Opus)
                if audio_path.lower().endswith(".ogg"):
                    audio_path = self._convert_ogg_to_wav(audio_path)

                logger.info(f"Echo transcribing audio file: {audio_path}")
                try:
                    transcript = await self.audio_llm.transcribe_audio(audio_path)
                    logger.info(f"Transcription result: {transcript}")
                except Exception as e:
                    logger.error(f"Failed to transcribe: {e}")
                    return TaskResult(
                        task_id=task.task_id, agent=self.name,
                        success=False, error=f"Transcription failed: {e}",
                    )
            elif has_audio and not self.audio_llm:
                return TaskResult(
                    task_id=task.task_id, agent=self.name,
                    success=False, error="Audio provider not configured.",
                )

            if not transcript:
                return TaskResult(
                    task_id=task.task_id, agent=self.name,
                    success=False, error="No audio or text to process.",
                )

            # 2. Re-route through ZeroClaw so the right agent handles intent
            await self._reroute_transcript(task, transcript)

            # 3. Also generate a voice confirmation / echo response
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": transcript},
            ]
            response_text = await self.llm.chat_completion(
                messages, temperature=0.7,
            )

            # 4. TTS Generation
            audio_path = await self._generate_tts(response_text)

            if audio_path:
                return TaskResult(
                    task_id=task.task_id,
                    agent=self.name,
                    success=True,
                    content_type=ContentType.VOICE,
                    text=response_text,
                    media_path=audio_path,
                )
            else:
                return TaskResult(
                    task_id=task.task_id, agent=self.name,
                    success=True, text=response_text,
                )

        except Exception as e:
            logger.exception("Echo processing failed")
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error=str(e),
            )
