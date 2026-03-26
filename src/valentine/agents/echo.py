# src/valentine/agents/echo.py
from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block
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
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")
        return (
            identity_block()
            + f"Current date and time: {time_str}\n\n"
            "Currently responding to a voice message. The user spoke to you and their "
            "words have been transcribed below.\n\n"
            "Respond naturally and conversationally, as if you're speaking back to them. "
            "Keep it warm, concise, and human — like a quick voice reply to a friend.\n\n"
            "IMPORTANT formatting rules (your output will be spoken via TTS):\n"
            "- Do NOT use markdown, asterisks, bold, or bullet points.\n"
            "- Do NOT use special characters or emoji.\n"
            "- Write in natural spoken English — contractions, casual phrasing.\n"
            "- Keep responses under 3-4 sentences unless the question demands depth.\n"
            "- You are Valentine. Be yourself."
        )

    # ------------------------------------------------------------------
    # Audio helpers
    # ------------------------------------------------------------------

    # Extensions that need ffmpeg conversion to WAV for Whisper
    _OGG_EXTENSIONS = {".ogg", ".oga", ".opus"}

    @staticmethod
    def _convert_to_wav(audio_path: str) -> str:
        """Convert OGG/OGA/OPUS audio to 16kHz mono WAV for Whisper."""
        wav_path = os.path.splitext(audio_path)[0] + ".wav"
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", audio_path,
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                wav_path,
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0 and os.path.exists(wav_path):
                logger.info(f"Converted {audio_path} → {wav_path}")
                return wav_path
            logger.warning(f"ffmpeg conversion failed: {proc.stderr}")
        except FileNotFoundError:
            logger.warning("ffmpeg not found — skipping audio conversion")
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg conversion timed out")
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
        return audio_path

    PIPER_MODEL = "/opt/valentine/models/piper/en_US-lessac-medium.onnx"

    async def _generate_tts(self, text: str) -> str:
        """Generate speech audio using Piper TTS (offline, CPU-optimized)."""
        import sys
        piper_bin = os.path.join(os.path.dirname(sys.executable), "piper")
        wav_path = os.path.join(
            settings.workspace_dir, f"response_{uuid.uuid4().hex[:8]}.wav",
        )
        ogg_path = wav_path.replace(".wav", ".ogg")
        try:
            # Piper reads text from stdin and writes WAV
            proc = subprocess.run(
                [piper_bin, "--model", self.PIPER_MODEL, "--output_file", wav_path],
                input=text, capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0 or not os.path.exists(wav_path):
                logger.error(f"Piper TTS failed: {proc.stderr}")
                return ""
            # Convert WAV → OGG (Telegram native voice format)
            ffmpeg_proc = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", ogg_path],
                capture_output=True, timeout=30,
            )
            if ffmpeg_proc.returncode == 0 and os.path.exists(ogg_path):
                os.remove(wav_path)
                return ogg_path
            # Fallback to WAV if ffmpeg conversion fails
            return wav_path
        except FileNotFoundError:
            logger.warning("piper or ffmpeg not found in PATH.")
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
        original = task.message
        # Include caption context if the user sent one with the voice note
        text = transcript
        if original.text:
            text = f"{original.text}\n\n[Voice message transcript]: {transcript}"

        rerouted_msg = IncomingMessage(
            message_id=original.message_id,
            chat_id=original.chat_id,
            user_id=original.user_id,
            platform=original.platform,
            content_type=ContentType.TEXT,
            text=text,
            user_name=original.user_name,
            reply_to_text=original.reply_to_text,
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
        chat_id = msg.chat_id

        has_audio = (
            msg.media_path is not None and msg.content_type == ContentType.VOICE
        )
        transcript = msg.text or ""

        # Check if this is a TTS-only request (text routed to Echo for speech)
        tts_only = not has_audio and transcript

        try:
            # 1. Transcription (only for audio messages)
            if has_audio:
                import asyncio
                audio_path = msg.media_path

                # Telegram voice = .oga/.ogg/.opus — convert to WAV for Whisper
                ext = os.path.splitext(audio_path)[1].lower()
                if ext in self._OGG_EXTENSIONS:
                    audio_path = self._convert_to_wav(audio_path)

                logger.info(f"Echo transcribing audio file: {audio_path}")
                try:
                    import sys
                    whisper_bin = os.path.join(os.path.dirname(sys.executable), "insanely-fast-whisper")
                    output_json = os.path.join(settings.workspace_dir, f"transcript_{uuid.uuid4().hex[:8]}.json")
                    cmd = [
                        whisper_bin,
                        "--file-name", audio_path,
                        "--device", "cpu",
                        "--model", "openai/whisper-base",
                        "--transcript-path", output_json
                    ]
                    
                    # Run STT directly using locally installed insanely-fast-whisper
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    await proc.communicate()
                    
                    if proc.returncode == 0 and os.path.exists(output_json):
                        with open(output_json, "r") as f:
                            data = json.load(f)
                            transcript = data.get("text", "")
                        os.remove(output_json)
                    else:
                        raise RuntimeError(f"Whisper STT failed. Check logs.")
                        
                    logger.info(f"Transcription result: {transcript}")
                except Exception as e:
                    logger.error(f"Failed to transcribe locally: {e}")
                    return TaskResult(
                        task_id=task.task_id, agent=self.name,
                        success=False, error="I couldn't understand that voice message. Try again?",
                    )

            if not transcript:
                return TaskResult(
                    task_id=task.task_id, agent=self.name,
                    success=False, error="I couldn't hear anything in that voice message.",
                )

            # TTS-only mode: generate speech from the provided text
            if tts_only:
                return await self._handle_tts_request(task, transcript)

            # Save transcription to history
            if chat_id:
                await self.bus.append_history(chat_id, "user", f"[Voice message] {transcript}")

            # Re-route through ZeroClaw so the right agent handles intent.
            # Echo does NOT also respond — the target agent sends the reply.
            # This prevents duplicate messages.
            await self._reroute_transcript(task, transcript)

            # Return a silent success — the re-routed agent will send the
            # actual response to the user. We return no text so the Telegram
            # adapter has nothing to send.
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, text="",
            )

        except Exception as e:
            logger.exception("Echo processing failed")
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error="Something went wrong processing your voice message.",
            )

    async def _handle_tts_request(self, task: AgentTask, text: str) -> TaskResult:
        """Generate a TTS voice response from text."""
        chat_id = task.message.chat_id

        # Generate spoken response via LLM
        history = await self.bus.get_history(chat_id) if chat_id else []
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history[-6:])  # last few messages for context
        messages.append({"role": "user", "content": text})

        response_text = None
        # Try primary LLM first, then fall back to a fresh FallbackChain
        for attempt_llm in [self.llm, None]:
            try:
                if attempt_llm is None:
                    # Lazy fallback — create a FallbackChain on demand
                    from valentine.llm import FallbackChain, GroqClient, CerebrasClient, SambaNovaClient
                    attempt_llm = FallbackChain([GroqClient(), CerebrasClient(), SambaNovaClient()])
                response_text = await attempt_llm.chat_completion(
                    messages, temperature=0.7,
                )
                break
            except Exception as e:
                logger.error(f"TTS LLM call failed ({getattr(attempt_llm, 'provider_name', '?')}): {e}")
                continue

        if not response_text:
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error="I'm having trouble right now. Try again in a moment.",
            )

        # Save response to history
        if chat_id:
            await self.bus.append_history(chat_id, "assistant", response_text[:500])

        # Generate TTS audio
        audio_path = await self._generate_tts(response_text)

        if audio_path:
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, content_type=ContentType.VOICE,
                text=response_text, media_path=audio_path,
            )
        else:
            # TTS failed — return text instead of an error
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, text=response_text,
            )
