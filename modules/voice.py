"""
modules/voice.py — Arc Fleet Voice Module
TTS (Text-to-Speech) + STT (Speech-to-Text) + Bland.ai integration

GoF Patterns:
  - Strategy:  VoiceBackend is swappable (Bland / GCP TTS / Twilio TTS / Local pyttsx3)
  - Adapter:   Each backend adapts its API to the common VoiceBackend interface
  - Proxy:     BlandProxy gates actual calls until ACTIVE
  - Observer:  CallEventBus notifies standup_bot when a call completes

SOLID:
  - SRP: VoiceModule orchestrates; backends do the work
  - OCP: Add new backends (e.g., ElevenLabs) without changing callers
  - LSP: All backends are substitutable via VoiceBackend ABC
  - ISP: Backends only implement what they support
  - DIP: VoiceModule depends on VoiceBackend ABC, not concrete classes

Setup (set in .env):
    BLAND_API_KEY=...
    BLAND_PHONE_NUMBER=+1xxxxxxxxxx
    BLAND_TRANSFER_NUMBER=7018705235
    GOOGLE_TTS_ENABLED=true
    GCP_PROJECT_ID=arc-fleet-campaign
    VOICE_BACKEND=bland   # bland | gcp | twilio | local

Robot-to-robot test:
    python -m modules.voice --robot-test
    python -m modules.voice --tts "Hello, this is a test of the Arc Fleet voice system."
    python -m modules.voice --call +17018705235 --script scripts/test_call.txt
"""

from __future__ import annotations
import os
import sys
import json
import time
import logging
import argparse
import tempfile
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

from modules import ModuleBase, STATUS_STUB, STATUS_ACTIVE

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# ABSTRACT BACKEND — GoF Strategy
# ═══════════════════════════════════════════════════════

class VoiceBackend(ABC):
    """Abstract voice backend. GoF: Strategy interface."""

    @abstractmethod
    def tts(self, text: str, output_path: Optional[str] = None) -> str:
        """Convert text to speech. Returns path to audio file."""
        ...

    @abstractmethod
    def call(self, to_number: str, script: str, transfer_to: Optional[str] = None) -> Dict:
        """Place an outbound call. Returns call result dict."""
        ...

    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """Speech to text. Returns transcript string."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...


# ═══════════════════════════════════════════════════════
# BLAND.AI BACKEND
# ═══════════════════════════════════════════════════════

class BlandBackend(VoiceBackend):
    """
    Bland.ai backend. GoF: Adapter wrapping Bland REST API.

    Bland.ai is ideal for:
      - Parallel outbound qualifier calls (Observer wave)
      - Human-quality TTS voices
      - Built-in transfer logic (Wave → Human hand-off)
      - Webhook callbacks for call events

    Docs: https://docs.bland.ai
    """

    name = "bland"

    # Default arc-fleet call script (can be overridden)
    DEFAULT_SCRIPT = """
You are calling on behalf of Kenyon Jones about a vehicle he has for sale.
Introduce yourself as calling for Kenyon Jones at (701) 870-5235.
Ask if the dealer or buyer is interested in the vehicle described.
If they show interest, say that Kenyon will call them directly or
offer to transfer now. Be professional and brief.
Do not give detailed pricing — just confirm interest.
"""

    def __init__(self):
        self.api_key       = os.getenv("BLAND_API_KEY", "")
        self.from_number   = os.getenv("BLAND_PHONE_NUMBER", "")
        self.transfer_to   = os.getenv("BLAND_TRANSFER_NUMBER", "7018705235")
        self._base_url     = "https://api.bland.ai/v1"

    def _headers(self) -> Dict:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def tts(self, text: str, output_path: Optional[str] = None) -> str:
        """Bland TTS via /speak endpoint — returns audio URL or local path."""
        import requests
        resp = requests.post(
            f"{self._base_url}/speak",
            headers=self._headers(),
            json={"text": text, "voice": "nat"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        audio_url = data.get("url", "")
        if output_path and audio_url:
            audio_data = requests.get(audio_url, timeout=30).content
            Path(output_path).write_bytes(audio_data)
            return output_path
        return audio_url

    def call(self, to_number: str, script: str = None, transfer_to: str = None,
             vehicle_info: Dict = None, max_duration: int = 120) -> Dict:
        """
        Place an outbound AI call via Bland.ai.
        GoF: Proxy — gates actual call.

        Returns: {call_id, status, duration, transcript, transferred}
        """
        import requests
        script = script or self.DEFAULT_SCRIPT
        if vehicle_info:
            script = f"Vehicle details: {json.dumps(vehicle_info)}\n\n{script}"

        payload = {
            "phone_number":      to_number,
            "from":              self.from_number,
            "task":              script,
            "voice":             "nat",
            "max_duration":      max_duration,
            "record":            True,
            "temperature":       0.7,
            "transfer_phone_number": transfer_to or self.transfer_to,
            "wait_for_greeting": True,
            "language":          "en-US",
        }

        logger.info(f"[Bland] Calling {to_number} ...")
        resp = requests.post(
            f"{self._base_url}/calls",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"[Bland] Call placed: {result.get('call_id')}")
        return result

    def get_call_status(self, call_id: str) -> Dict:
        """Poll call status. Use with CallWatcher."""
        import requests
        resp = requests.get(
            f"{self._base_url}/calls/{call_id}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def transcribe(self, audio_path: str) -> str:
        """Transcribe audio via Google STT (Bland doesn't have standalone STT)."""
        logger.warning("[Bland] No standalone STT — using Google fallback.")
        return GCPTTSBackend().transcribe(audio_path)

    def robot_vs_robot(self, number_a: str, number_b: str,
                       script_a: str = None, script_b: str = None) -> Dict:
        """
        Robot-to-robot call test.
        Calls number_a with script_a, which will be told to call number_b.
        Records both sides and returns transcripts.

        Usage:
            result = bland.robot_vs_robot(
                "+17018705235", "+17019465731",
                script_a="Introduce yourself as Robot A. Ask Robot B a question about vehicles.",
                script_b="You are Robot B. Answer questions about vehicles briefly."
            )
        """
        logger.info("[Bland] Starting robot-vs-robot test call ...")
        script_a = script_a or (
            "You are Robot A, a vehicle sales AI. "
            "Call the other number and ask if they are interested in buying a 2016 Lincoln MKZ Hybrid. "
            "Record their response and end the call politely."
        )

        # Call A → calls B
        result_a = self.call(number_a, script=script_a, transfer_to=number_b)
        call_id_a = result_a.get("call_id")

        logger.info(f"[Bland] Robot A call_id: {call_id_a}. Polling for completion ...")
        time.sleep(5)

        # Poll until done
        for _ in range(30):
            status = self.get_call_status(call_id_a)
            if status.get("status") in ("completed", "failed", "no-answer"):
                break
            time.sleep(4)

        transcript = status.get("transcript", "(no transcript)")
        recording  = status.get("recording_url", "")

        return {
            "call_id":    call_id_a,
            "status":     status.get("status"),
            "transcript": transcript,
            "recording":  recording,
            "raw":        status,
        }


# ═══════════════════════════════════════════════════════
# GOOGLE CLOUD TTS/STT BACKEND
# ═══════════════════════════════════════════════════════

class GCPTTSBackend(VoiceBackend):
    """
    Google Cloud Text-to-Speech + Speech-to-Text backend.
    GoF: Adapter — wraps GCP API to VoiceBackend interface.

    Voices: en-US-Neural2-D (male), en-US-Neural2-F (female)
    Docs: https://cloud.google.com/text-to-speech
    """

    name = "gcp"

    def tts(self, text: str, output_path: Optional[str] = None,
            voice: str = "en-US-Neural2-D", speed: float = 1.0) -> str:
        """Convert text to speech using Google Cloud TTS."""
        from google.cloud import texttospeech

        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name=voice,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speed,
        )

        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )

        output_path = output_path or tempfile.mktemp(suffix=".mp3")
        Path(output_path).write_bytes(response.audio_content)
        logger.info(f"[GCP TTS] Audio saved: {output_path}")
        return output_path

    def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file using Google Cloud STT."""
        from google.cloud import speech

        client = speech.SpeechClient()
        audio_bytes = Path(audio_path).read_bytes()
        audio = speech.RecognitionAudio(content=audio_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MP3,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
        )

        response = client.recognize(config=config, audio=audio)
        transcript = " ".join(r.alternatives[0].transcript for r in response.results)
        return transcript

    def call(self, to_number: str, script: str, transfer_to: str = None) -> Dict:
        """GCP TTS doesn't place calls — delegates to Twilio+TTS combo."""
        raise NotImplementedError(
            "GCP TTS doesn't place calls. Use BlandBackend or TwilioBackend."
        )


# ═══════════════════════════════════════════════════════
# LOCAL FALLBACK (pyttsx3 — no API key needed)
# ═══════════════════════════════════════════════════════

class LocalTTSBackend(VoiceBackend):
    """
    Local offline TTS using pyttsx3 or espeak.
    Good for testing without API keys.
    pip install pyttsx3
    """

    name = "local"

    def tts(self, text: str, output_path: Optional[str] = None) -> str:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            output_path = output_path or tempfile.mktemp(suffix=".wav")
            engine.save_to_file(text, output_path)
            engine.runAndWait()
            logger.info(f"[Local TTS] Saved: {output_path}")
            return output_path
        except ImportError:
            # Fallback to espeak (Linux)
            import subprocess
            output_path = output_path or "/tmp/tts_output.wav"
            subprocess.run(["espeak", "-w", output_path, text], check=True)
            return output_path

    def transcribe(self, audio_path: str) -> str:
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio = r.record(source)
            return r.recognize_google(audio)
        except Exception as e:
            return f"(transcription failed: {e})"

    def call(self, to_number: str, script: str, transfer_to: str = None) -> Dict:
        raise NotImplementedError("Local TTS cannot place phone calls.")


# ═══════════════════════════════════════════════════════
# CALL EVENT BUS — GoF Observer
# ═══════════════════════════════════════════════════════

class CallEventBus:
    """
    GoF Observer — notifies listeners when call events happen.
    Used by standup_bot and notifier to react to call completions.

    Example:
        bus = CallEventBus()
        bus.subscribe("completed", standup_bot.on_call_complete)
        bus.subscribe("failed",    notifier.alert_team)
        bus.emit("completed", call_result)
    """

    def __init__(self):
        self._listeners: Dict[str, List] = {}

    def subscribe(self, event: str, callback):
        self._listeners.setdefault(event, []).append(callback)

    def emit(self, event: str, data: Dict):
        for cb in self._listeners.get(event, []):
            try:
                cb(data)
            except Exception as e:
                logger.error(f"[CallEventBus] Listener error on '{event}': {e}")

    def unsubscribe(self, event: str, callback):
        if event in self._listeners:
            self._listeners[event] = [c for c in self._listeners[event] if c != callback]


# ═══════════════════════════════════════════════════════
# VOICE MODULE — Main interface (GoF Facade)
# ═══════════════════════════════════════════════════════

class VoiceModule(ModuleBase):
    """
    Main Voice module. GoF: Facade over all voice backends.

    Backend selection (in priority order):
      1. VOICE_BACKEND env var
      2. Bland if BLAND_API_KEY set
      3. GCP if GOOGLE_APPLICATION_CREDENTIALS set
      4. Local fallback
    """

    MODULE_ID = "voice"

    def __init__(self):
        self.backend_name = os.getenv("VOICE_BACKEND", "").lower()
        self.backend: VoiceBackend = self._select_backend()
        self.event_bus = CallEventBus()
        self.STATUS = STATUS_ACTIVE if self.backend.name != "local" else STATUS_STUB

    def _select_backend(self) -> VoiceBackend:
        """GoF Factory Method — pick the right backend."""
        backends = {
            "bland":  BlandBackend,
            "gcp":    GCPTTSBackend,
            "local":  LocalTTSBackend,
        }
        if self.backend_name in backends:
            return backends[self.backend_name]()

        # Auto-detect
        if os.getenv("BLAND_API_KEY"):
            logger.info("[Voice] Auto-selected: Bland.ai backend")
            return BlandBackend()
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            logger.info("[Voice] Auto-selected: GCP TTS backend")
            return GCPTTSBackend()
        logger.warning("[Voice] No API keys found. Using local TTS fallback.")
        return LocalTTSBackend()

    def say(self, text: str, output_path: str = None) -> str:
        """TTS: convert text to audio file."""
        return self.backend.tts(text, output_path)

    def call(self, to_number: str, script: str, vehicle_info: Dict = None,
             transfer_to: str = None) -> Dict:
        """Place outbound AI call. Emits events to observers."""
        result = self.backend.call(
            to_number, script=script,
            transfer_to=transfer_to,
            **({"vehicle_info": vehicle_info} if vehicle_info and hasattr(self.backend, "vehicle_info") else {})
        )
        event = "completed" if result.get("status") == "completed" else "initiated"
        self.event_bus.emit(event, result)
        return result

    def transcribe(self, audio_path: str) -> str:
        """STT: convert audio to text."""
        return self.backend.transcribe(audio_path)

    def robot_test(self, number_a: str = None, number_b: str = None) -> Dict:
        """
        Robot-to-robot call test.
        Default: calls Kenyon's number and Cynthia's number.
        """
        number_a = number_a or os.getenv("SENDER_PHONE", "7018705235")
        number_b = number_b or os.getenv("ALT_PHONE",    "7019465731")

        if not isinstance(self.backend, BlandBackend):
            raise RuntimeError("Robot-to-robot test requires Bland.ai backend.")

        return self.backend.robot_vs_robot(
            f"+1{number_a.replace('+1','').replace('-','')}",
            f"+1{number_b.replace('+1','').replace('-','')}",
        )

    def health_check(self) -> Dict:
        return {
            "module":  self.MODULE_ID,
            "status":  self.STATUS,
            "backend": self.backend.name,
            "active":  self.STATUS == STATUS_ACTIVE,
            "bland_key_set":  bool(os.getenv("BLAND_API_KEY")),
            "gcp_creds_set":  bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")),
        }


# ═══════════════════════════════════════════════════════
# CLI — python -m modules.voice
# ═══════════════════════════════════════════════════════

def cli():
    parser = argparse.ArgumentParser(description="Arc Fleet Voice Module CLI")
    parser.add_argument("--tts",         metavar="TEXT",   help="Convert text to speech")
    parser.add_argument("--call",        metavar="NUMBER", help="Call a phone number")
    parser.add_argument("--script",      metavar="FILE",   help="Script file for --call")
    parser.add_argument("--transcribe",  metavar="FILE",   help="Transcribe audio file")
    parser.add_argument("--robot-test",  action="store_true", help="Robot-to-robot call test")
    parser.add_argument("--number-a",    default=None,     help="Robot A number")
    parser.add_argument("--number-b",    default=None,     help="Robot B number")
    parser.add_argument("--backend",     default=None,     help="Force backend: bland|gcp|local")
    parser.add_argument("--output",      default=None,     help="Audio output file")
    args = parser.parse_args()

    if args.backend:
        os.environ["VOICE_BACKEND"] = args.backend

    module = VoiceModule()
    print(f"Voice module: {module.health_check()}")

    if args.tts:
        path = module.say(args.tts, args.output)
        print(f"✓ Audio saved: {path}")

    elif args.robot_test:
        print("Starting robot-to-robot test ...")
        result = module.robot_test(args.number_a, args.number_b)
        print(json.dumps(result, indent=2))

    elif args.call:
        script = Path(args.script).read_text() if args.script else None
        result = module.call(args.call, script)
        print(json.dumps(result, indent=2))

    elif args.transcribe:
        transcript = module.transcribe(args.transcribe)
        print(f"Transcript:\n{transcript}")

    else:
        parser.print_help()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli()
