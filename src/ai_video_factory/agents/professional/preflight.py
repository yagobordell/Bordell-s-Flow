"""Zero-API-cost checks for assets required by the actual source script.

A deterministic strong-signal check catches explicit ebook promotions that
require the authentic cover and QR compositor. It never rewrites source beats,
decides visual rhythm, or substitutes a generic ebook asset.
"""
from __future__ import annotations

import re

_EBOOK = re.compile(r"\b(?:e[ -]?book|digital book|gu[ií]a digital)\b", re.I)
_QR = re.compile(r"\bQR(?:\s+code|\s+c[oó]digo)?\b", re.I)
_SCAN = re.compile(r"\b(?:scan|scann?ing|escane(?:a|ar|e)|esc[aá]nealo)\b", re.I)


def requires_ebook_cover(script: str) -> bool:
    """Detect a clear *spoken ebook promotion with QR instruction*.

    This is a strong-signal asset dependency, not a general CTA classifier:
    weaker or ambiguous promotional language remains the director's job.
    """
    ebook = list(_EBOOK.finditer(script))
    if not ebook:
        return False
    for qr in _QR.finditer(script):
        nearby = script[max(0, qr.start() - 2200):qr.end() + 2200]
        if _EBOOK.search(nearby) and _SCAN.search(nearby):
            return True
    return False


def validate_source_assets(script: str, *, ebook_available: bool) -> None:
    """Reject an impossible authentic ebook CTA *before* calling the LLM."""
    if not ebook_available and requires_ebook_cover(script):
        raise ValueError(
            "PREFLIGHT_EBOOK_COVER_REQUIRED: el guion promociona un ebook "
            "y pide escanear un QR, pero falta la portada auténtica. "
            "Configura --ebook-cover o la portada guardada del avatar antes "
            "de ejecutar Profesional. No se ha llamado a OpenAI ni se ha "
            "alterado el CTA original."
        )
