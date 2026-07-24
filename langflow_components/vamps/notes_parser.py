"""
VAMPS · NotesParser — Langflow 1.7.3 custom component.

Port of the FastAPI `/upload_notes` endpoint. Reads pre-existing meeting notes
or a spec (.docx, .txt, .md) and returns their plain text, to seed the spec
generator.
"""
# Flat top-level imports required by Langflow's flow loader (see spec_generator.py).
from langflow.custom import Component
from langflow.io import FileInput, Output
from langflow.schema.data import Data
from langflow.schema.message import Message

import io
from pathlib import Path

from docx import Document


class NotesParser(Component):
    display_name = "VAMPS Notes Parser"
    description = "Extract plain text from an uploaded .docx / .txt / .md notes or spec file."
    documentation = "https://github.com/djj067/vamps"
    icon = "upload"
    name = "vamps_notes_parser"

    inputs = [
        FileInput(
            name="notes_file",
            display_name="Notes File",
            info="Pre-existing notes or spec to seed from.",
            file_types=["docx", "txt", "md", "markdown"],
            required=True,
        ),
    ]

    outputs = [
        Output(name="text", display_name="Text", method="build_text"),
        Output(name="result", display_name="Full Result (Data)", method="build_result"),
    ]

    def _run(self) -> dict:
        if getattr(self, "_computed", None) is not None:
            return self._computed

        path = self.notes_file
        if isinstance(path, list):
            path = path[0] if path else None
        if not path:
            raise ValueError("NotesParser: no file provided.")

        p = Path(path)
        data = p.read_bytes()
        name = p.name
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext == "docx":
            doc = Document(io.BytesIO(data))
            text = "\n".join(par.text for par in doc.paragraphs)
        else:
            text = data.decode("utf-8", errors="replace")

        text = text.strip()
        if not text:
            raise ValueError(f"NotesParser: '{name}' had no readable text.")

        self._computed = {"ok": True, "name": name, "text": text, "chars": len(text)}
        return self._computed

    def build_text(self) -> Message:
        result = self._run()
        self.status = f'{result["name"]} ({result["chars"]} chars)'
        return Message(text=result["text"])

    def build_result(self) -> Data:
        result = self._run()
        self.status = {"name": result["name"], "chars": result["chars"]}
        return Data(data=result)
