"""Local-only receipt OCR and optional voice replies.

These adapters never write financial records or contact an external service.
The caller owns receipt confirmation and the user's opt-in for voice replies.
"""
from app.media.receipts import (
    ReceiptDraft,
    ReceiptError,
    ReceiptItem,
    ReceiptService,
    ReceiptUnavailable,
    image_dimensions,
    parse_receipt_text,
    receipt_source_key,
)
from app.media.voice import LocalVoiceService, VoiceError, VoiceReply, VoiceUnavailable

__all__ = [
    "ReceiptDraft", "ReceiptError", "ReceiptItem", "ReceiptService",
    "ReceiptUnavailable", "image_dimensions", "parse_receipt_text",
    "receipt_source_key", "LocalVoiceService", "VoiceError", "VoiceReply",
    "VoiceUnavailable",
]
