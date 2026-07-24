"""
VAMPS · DeepgramTranscribe — Langflow 1.7.3 custom component.

Port of the FastAPI `/transcribe` endpoint. Takes an uploaded audio file and
returns the transcribed text via Deepgram's pre-recorded API.

Note: this transcribes a whole audio file per run. The original app's real-time
"record the mic in short segments" behaviour is a browser/transport concern that
lives in the UI calling the flow, not in Langflow.
"""
# Flat top-level imports required by Langflow's flow loader (see spec_generator.py).
from langflow.custom import Component
from langflow.io import FileInput, MessageTextInput, Output, SecretStrInput
from langflow.schema.data import Data
from langflow.schema.message import Message

import mimetypes
import os
from pathlib import Path

import httpx

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class DeepgramTranscribe(Component):
    display_name = "VAMPS Deepgram Transcribe"
    description = "Transcribe an audio file to text using Deepgram's pre-recorded API."
    documentation = "https://github.com/djj067/vamps"
    icon = "mic"
    name = "vamps_deepgram_transcribe"

    inputs = [
        FileInput(
            name="audio_file",
            display_name="Audio File",
            info="Audio to transcribe (webm/wav/mp3/m4a...).",
            file_types=["webm", "wav", "mp3", "m4a", "ogg", "flac", "aac"],
            required=True,
        ),
        SecretStrInput(
            name="deepgram_api_key",
            display_name="Deepgram API Key",
            info="Leave blank to use the DEEPGRAM_API_KEY environment variable.",
            required=False,
        ),
        MessageTextInput(name="model", display_name="Model", value="nova-3"),
        MessageTextInput(name="language", display_name="Language", value="en"),
        MessageTextInput(
            name="keyterms",
            display_name="Key Terms",
            info="Comma-separated domain terms to bias toward (nova-3 keyterm prompting).",
            value="",
        ),
    ]

    outputs = [
        Output(name="text", display_name="Transcript", method="build_text"),
        Output(name="result", display_name="Full Result (Data)", method="build_result"),
    ]

    def _run(self) -> dict:
        if getattr(self, "_computed", None) is not None:
            return self._computed

        api_key = (self.deepgram_api_key or "").strip() or os.getenv("DEEPGRAM_API_KEY", "")
        if not api_key:
            raise ValueError("DeepgramTranscribe: no Deepgram API key (input or DEEPGRAM_API_KEY).")

        path = self.audio_file
        if isinstance(path, list):  # FileInput may hand back a list of paths
            path = path[0] if path else None
        if not path:
            raise ValueError("DeepgramTranscribe: no audio file provided.")
        data = Path(path).read_bytes()

        params = {"model": self.model, "language": self.language, "smart_format": "true"}
        keyterms = [t.strip() for t in (self.keyterms or "").split(",") if t.strip()]
        if keyterms:
            params["keyterm"] = keyterms
        content_type = mimetypes.guess_type(str(path))[0] or "audio/webm"

        with httpx.Client(timeout=120) as http:
            resp = http.post(
                DEEPGRAM_URL, params=params, content=data,
                headers={"Authorization": f"Token {api_key}", "Content-Type": content_type},
            )
        resp.raise_for_status()
        body = resp.json()
        alt = body["results"]["channels"][0]["alternatives"][0]
        text = (alt.get("transcript") or "").strip()

        self._computed = {
            "ok": True,
            "text": text,
            "model": self.model,
            "confidence": alt.get("confidence"),
            "size_kb": round(len(data) / 1024, 1),
        }
        return self._computed

    def build_text(self) -> Message:
        result = self._run()
        self.status = result["text"]
        return Message(text=result["text"])

    def build_result(self) -> Data:
        result = self._run()
        self.status = result
        return Data(data=result)
