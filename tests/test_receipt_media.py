"""Receipt drafts never guess missing totals or write financial records."""
import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
import struct
import zlib

import pytest

from app.media import ReceiptError, ReceiptService, ReceiptUnavailable, image_dimensions, parse_receipt_text, receipt_source_key
from app.media._process import ProcessFailure


def _png(width=10, height=10):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    data = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))


def test_argentine_receipt_uses_explicit_total_not_cash_tax_or_subtotal():
    draft = parse_receipt_text("""SUPERMERCADO DEMO
FECHA 02/11/2026
Manzanas 1.000,50
Pan 249,50
SUBTOTAL 1.250,00
IVA 216,94
TOTAL A PAGAR ARS 1.250,00
EFECTIVO 2.000,00
VUELTO 750,00
""", today=date(2026, 11, 2))
    assert draft.name == "SUPERMERCADO DEMO"
    assert draft.amount == Decimal("1250.00")
    assert draft.currency == "ARS"
    assert draft.expense_date == date(2026, 11, 2)
    assert [(item.name, item.amount) for item in draft.items] == [
        ("Manzanas", Decimal("1000.50")), ("Pan", Decimal("249.50")),
    ]
    assert draft.needs_confirmation is True
    assert draft.source == "local_ocr"


@pytest.mark.parametrize("amount,expected", [
    ("1.234,56", "1234.56"), ("1,234.56", "1234.56"),
    ("1 234,56", "1234.56"), ("1234", "1234.00"),
    ("1.234.567,89", "1234567.89"), ("12.5", "12.50"),
    ("1.000", "1000.00"), ("12,50", "12.50"),
])
def test_total_money_parsing_is_exact(amount, expected):
    draft = parse_receipt_text(f"COMERCIO\nTOTAL ARS {amount}")
    assert draft.amount == Decimal(expected)
    assert draft.needs_confirmation


@pytest.mark.parametrize("text", [
    "TIENDA\nProducto 10,00\nProducto 20,00",  # Never sum items as a substitute for TOTAL.
    "TIENDA\nSUBTOTAL 100,00\nIVA 21,00\nEFECTIVO 200,00",
    "TIENDA\nTOTAL -$ 100,00",
    "TIENDA\nTOTAL ($100,00)",
    "TIENDA\nTOTAL -USD 100,00",
    "TIENDA\nTOTAL -R$ 100,00",
    "TIENDA\nTOTAL (EUR 100,00)",
    "TIENDA\nTOTAL $ 0,00",
    "TIENDA\nTOTAL 1.OOO,50",
    "TIENDA\nTOTAL 10,00 20,00",
    "TIENDA\nTOTAL 100,00\nTOTAL 200,00",
    "TIENDA\nTOTAL USD 100,00 ARS 100,00",
])
def test_ambiguous_or_invalid_total_remains_missing(text):
    draft = parse_receipt_text(text)
    assert draft.amount is None
    assert draft.warnings
    assert draft.needs_confirmation is True


def test_final_total_has_precedence_over_prior_total_and_duplicate_is_not_ambiguous():
    assert parse_receipt_text("TOTAL 120\nTOTAL FINAL 100").amount == Decimal("100")
    assert parse_receipt_text("TOTAL 120,00\nTOTAL 120,00").amount == Decimal("120")


def test_total_on_next_line_is_read_without_searching_arbitrary_following_numbers():
    assert parse_receipt_text("TOTAL\n$ 123,45").amount == Decimal("123.45")
    assert parse_receipt_text("TOTAL\nCUIT 12345678901").amount is None


@pytest.mark.parametrize("currency,marker", [("USD", "U$S"), ("USD", "USD"), ("BRL", "R$"), ("EUR", "€"), ("ARS", "ARS")])
def test_currency_on_total_has_priority_over_other_printed_currency(currency, marker):
    draft = parse_receipt_text(f"COMERCIO\nCotizacion ARS 1000\nTOTAL {marker} 20,50")
    assert draft.currency == currency
    assert draft.amount == Decimal("20.50")


def test_explicit_multiple_currencies_require_correction():
    draft = parse_receipt_text("COMERCIO\nTOTAL USD ARS 20,50")
    assert draft.amount is None
    assert any("monedas" in warning for warning in draft.warnings)


def test_no_date_is_invented_when_missing_invalid_ambiguous_or_future():
    for text in ("TOTAL 50", "FECHA 31/02/2026\nTOTAL 50",
                 "FECHA 01/11/2026\nFECHA 02/11/2026\nTOTAL 50",
                 "FECHA 03/11/2026\nTOTAL 50"):
        assert parse_receipt_text(text, today=date(2026, 11, 2)).expense_date is None


def test_expiry_date_does_not_override_receipt_date_and_caption_is_only_description():
    draft = parse_receipt_text("TIENDA\nFECHA 2026-11-02\nVencimiento 2027-01-01\nTOTAL 50", caption="Compra del día, 900 pesos")
    assert draft.expense_date == date(2026, 11, 2)
    assert draft.amount == Decimal("50")
    assert draft.name == "Compra del día, 900 pesos"


@pytest.mark.parametrize("text", ["", "   ", "X" * (128 * 1024 + 1)],
                         ids=["empty", "whitespace", "over_128_kb"])
def test_missing_or_excessive_text_is_rejected(text):
    with pytest.raises(ReceiptError):
        parse_receipt_text(text)


def test_photo_identity_is_stable_and_supports_group_chat_ids():
    assert receipt_source_key(-1234567890123, 42) == "receipt:-1234567890123:42"
    assert receipt_source_key(123, 42) == receipt_source_key(123, 42)
    for chat, message in ((0, 1), (True, 1), (123, 0), (123, -1), (123, "1")):
        with pytest.raises(ReceiptError):
            receipt_source_key(chat, message)


def test_jpeg_and_png_dimensions_are_checked_before_ocr():
    assert image_dimensions(_png(10, 20)) == (10, 20, ".png")
    # Minimal JPEG SOF segment with 3 components; native OCR owns full decode validation.
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x14\x00\x0a\x03" + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00" + b"\xff\xd9"
    assert image_dimensions(jpeg) == (10, 20, ".jpg")


@pytest.mark.parametrize("image", [b"", b"GIF89a\x00", b"\xff\xd8\xff\xc0\x00\xff", b"\x89PNG\r\n\x1a\n", b"x" * (10 * 1024 * 1024 + 1)],
                         ids=["empty", "unsupported_gif", "truncated_jpeg", "truncated_png", "over_10_mb"])
def test_invalid_payloads_are_rejected_before_ocr(image):
    with pytest.raises(ReceiptError):
        image_dimensions(image)


def test_decompression_dimensions_are_bounded():
    image = bytearray(_png())
    image[16:24] = struct.pack(">II", 10_000, 10_000)
    with pytest.raises(ReceiptError, match="resolución"):
        image_dimensions(bytes(image))


@pytest.mark.asyncio
async def test_ocr_uses_local_tool_and_cleans_image_and_output(monkeypatch):
    from app.media import receipts
    paths = []
    async def run(args, **kwargs):
        source, destination = Path(args[1]), Path(args[2])
        paths.extend((source, destination.with_suffix(".txt")))
        assert source.read_bytes() == _png()
        assert args[0] == "tesseract"
        assert args[3:] == ["-l", "spa+eng+por", "--psm", "6"]
        destination.with_suffix(".txt").write_text("TIENDA\nFECHA 02/11/2026\nTOTAL 25,50")
    monkeypatch.setattr(receipts, "run_local_process", run)
    result = await ReceiptService().analyze_receipt(_png(), today=date(2026, 11, 2))
    assert result.amount == Decimal("25.50")
    assert result.needs_confirmation is True
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(FileNotFoundError(), ReceiptUnavailable), (asyncio.TimeoutError(), ReceiptUnavailable), (ProcessFailure(1), ReceiptError), (asyncio.CancelledError(), asyncio.CancelledError)])
async def test_ocr_errors_and_cancellation_cleanup_temporary_image(monkeypatch, error, expected):
    from app.media import receipts
    paths = []
    async def run(args, **kwargs):
        paths.append(Path(args[1]))
        raise error
    monkeypatch.setattr(receipts, "run_local_process", run)
    with pytest.raises(expected):
        await ReceiptService().analyze_receipt(_png())
    assert paths and not paths[0].exists()


@pytest.mark.asyncio
async def test_ocr_does_not_accept_oversized_output(monkeypatch):
    from app.media import receipts
    async def run(args, **kwargs):
        Path(args[2]).with_suffix(".txt").write_bytes(b"x" * (128 * 1024 + 1))
    monkeypatch.setattr(receipts, "run_local_process", run)
    with pytest.raises(ReceiptError):
        await ReceiptService().analyze_receipt(_png())
