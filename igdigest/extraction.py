"""Text extraction from media: OCR for images, transcription for videos/reels.

Both are optional. Media is downloaded into a temporary directory that is deleted as soon
as the post has been processed. Whatever cannot be processed is recorded in the notes and
the post's coverage is marked partial.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .providers.base import Provider, ProviderError, RawPost

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
OPENAI_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024


@dataclass
class ExtractionResult:
    status: str  # full | partial | caption_only | none
    ocr_text: str | None = None
    transcript: str | None = None
    notes: list[str] = field(default_factory=list)


class Extractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ocr_problem = _ocr_unavailable_reason(settings)
        self._asr_problem = _asr_unavailable_reason(settings)

    def capabilities(self) -> dict[str, str]:
        return {
            "ocr": "enabled" if not self._ocr_problem else f"off ({self._ocr_problem})",
            "transcription": "enabled" if not self._asr_problem else f"off ({self._asr_problem})",
        }

    def extract(self, post: RawPost, provider: Provider) -> ExtractionResult:
        notes: list[str] = []
        ocr_parts: list[str] = []
        transcript_parts: list[str] = []
        media = post.media[: self.settings.max_media_per_post]
        skipped = len(post.media) - len(media)
        processed = 0

        with tempfile.TemporaryDirectory(prefix="igd-media-", ignore_cleanup_errors=True) as tmp:
            for idx, m in enumerate(media, start=1):
                label = f"media {idx} ({m.kind})"
                if m.fixture_text is not None:
                    target = ocr_parts if m.kind == "image" else transcript_parts
                    if m.fixture_text:
                        target.append(f"[fixture] {m.fixture_text}")
                    processed += 1
                    continue
                problem = self._ocr_problem if m.kind == "image" else self._asr_problem
                if problem:
                    notes.append(f"{label}: not processed, {'OCR' if m.kind == 'image' else 'transcription'} {problem}")
                    continue
                if not m.url:
                    notes.append(f"{label}: {m.note or 'no media URL available'}")
                    continue
                try:
                    path = provider.download(m, Path(tmp), self.settings.max_media_bytes)
                    if m.kind == "image":
                        text = self._ocr(path)
                        if text:
                            ocr_parts.append(text)
                    else:
                        text = self._transcribe(path)
                        if text:
                            transcript_parts.append(text)
                    processed += 1
                except ProviderError as e:
                    notes.append(f"{label}: download failed ({e})")
                except Exception as e:  # extraction backends raise many kinds of errors
                    log.warning("Extraction failed for %s %s: %s", post.url, label, type(e).__name__)
                    notes.append(f"{label}: extraction failed ({type(e).__name__})")
                finally:
                    for f in Path(tmp).iterdir():
                        f.unlink(missing_ok=True)

        if skipped:
            notes.append(f"{skipped} more media item(s) not processed (limit {self.settings.max_media_per_post} per post)")

        total = len(post.media)
        if total and processed == total:
            status = "full"
        elif processed:
            status = "partial"
        elif post.caption:
            status = "caption_only"
        else:
            status = "none"
        if total == 0 and not post.caption:
            notes.append("no caption and no media were available")
        return ExtractionResult(
            status=status,
            ocr_text=_join(ocr_parts),
            transcript=_join(transcript_parts),
            notes=notes,
        )

    # --- backends ------------------------------------------------------------
    def _ocr(self, path: Path) -> str:
        import pytesseract
        from PIL import Image

        if self.settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.settings.tesseract_cmd
        with Image.open(path) as img:
            return pytesseract.image_to_string(img, lang=self.settings.ocr_languages).strip()

    def _transcribe(self, path: Path) -> str:
        s = self.settings
        if s.transcribe_provider == "openai":
            if path.stat().st_size > OPENAI_TRANSCRIBE_MAX_BYTES:
                raise ValueError("file exceeds the transcription API size limit")
            from openai import OpenAI

            client = OpenAI(
                api_key=s.transcribe_api_key.get_secret_value() if s.transcribe_api_key else None,
                base_url=s.transcribe_base_url,
            )
            with path.open("rb") as fh:
                result = client.audio.transcriptions.create(
                    model=s.transcribe_model or "gpt-4o-mini-transcribe", file=fh
                )
            return (result.text or "").strip()
        if s.transcribe_provider == "faster_whisper":
            model = _whisper_model(s.transcribe_model or "small")
            segments, _info = model.transcribe(str(path))
            return " ".join(seg.text.strip() for seg in segments).strip()
        raise RuntimeError("transcription disabled")


def _join(parts: list[str]) -> str | None:
    text = "\n\n".join(p for p in parts if p).strip()
    return text[:MAX_TEXT_CHARS] if text else None


def _ocr_unavailable_reason(s: Settings) -> str | None:
    if not s.ocr_enabled:
        return "is not enabled (IGD_OCR_ENABLED)"
    try:
        import pytesseract
        import PIL  # noqa: F401

        if s.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = s.tesseract_cmd
        pytesseract.get_tesseract_version()
    except ImportError:
        return "dependencies missing (pip install -e .[ocr])"
    except Exception:
        return "Tesseract binary not found (install it or set IGD_TESSERACT_CMD)"
    return None


def _asr_unavailable_reason(s: Settings) -> str | None:
    if s.transcribe_provider == "none":
        return "is not configured (IGD_TRANSCRIBE_PROVIDER)"
    if s.transcribe_provider == "openai":
        try:
            import openai  # noqa: F401
        except ImportError:
            return "dependency missing (pip install -e .[openai])"
        import os

        if not s.transcribe_api_key and not os.environ.get("OPENAI_API_KEY"):
            return "has no API key (IGD_TRANSCRIBE_API_KEY)"
    if s.transcribe_provider == "faster_whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "dependency missing (pip install -e .[transcribe-local])"
    return None


@lru_cache(maxsize=1)
def _whisper_model(name: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, compute_type="int8")
