"""Conservative local receipt OCR: extraction produces a reviewable draft.

Only an explicit, unambiguous TOTAL is proposed. There is no inference from
item sums, no LLM repair of digits and no financial persistence in this module.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
import struct
import tempfile
import unicodedata

from app.media._process import ProcessFailure, run_local_process


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_EDGE = 12_000
MAX_OCR_BYTES = 128 * 1024
MAX_AMOUNT = Decimal("9999999999999999.99")


class ReceiptError(ValueError):
    """The image or extracted content cannot be used as a receipt draft."""


class ReceiptUnavailable(ReceiptError):
    """Local OCR is unavailable; it is safe to enter the expense manually."""


@dataclass(frozen=True)
class ReceiptItem:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class ReceiptDraft:
    name: str
    amount: Decimal | None
    currency: str
    expense_date: date | None
    items: tuple[ReceiptItem, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_text: str = ""
    needs_confirmation: bool = True
    source: str = "local_ocr"


def receipt_source_key(chat_id: int, message_id: int) -> str:
    """Stable Telegram-photo identity, independent of OCR output or retries."""
    if type(chat_id) is not int or type(message_id) is not int or not chat_id or message_id <= 0:
        raise ReceiptError("La foto no tiene una identidad de Telegram válida.")
    return f"receipt:{chat_id}:{message_id}"


def image_dimensions(image: bytes) -> tuple[int, int, str]:
    """Inspect JPEG/PNG dimensions before handing compressed data to OCR."""
    if not isinstance(image, bytes) or not image:
        raise ReceiptError("No recibí una imagen. Mandá una foto del ticket.")
    if len(image) > MAX_IMAGE_BYTES:
        raise ReceiptError("La imagen supera 10 MB. Mandá una foto más pequeña.")
    width = height = 0
    suffix = ""
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(image) < 33 or image[8:16] != b"\x00\x00\x00\rIHDR":
            raise ReceiptError("El PNG está incompleto o no es válido.")
        width, height = struct.unpack(">II", image[16:24])
        suffix = ".png"
    elif image.startswith(b"\xff\xd8"):
        pos = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while pos < len(image):
            if image[pos] != 0xFF:
                break
            while pos < len(image) and image[pos] == 0xFF:
                pos += 1
            if pos >= len(image):
                break
            marker = image[pos]
            pos += 1
            if marker in {0xD9, 0xDA}:  # End/scan before a dimensions header.
                break
            if marker in {0x01, 0xD8, *range(0xD0, 0xD8)}:
                continue
            if pos + 2 > len(image):
                break
            size = struct.unpack(">H", image[pos:pos + 2])[0]
            if size < 2 or pos + size > len(image):
                break
            if marker in sof_markers:
                if size < 8:
                    break
                height, width = struct.unpack(">HH", image[pos + 3:pos + 7])
                suffix = ".jpg"
                break
            pos += size
        if not suffix:
            raise ReceiptError("El JPEG está incompleto o no es válido.")
    else:
        raise ReceiptError("Mandá una foto JPEG o PNG del ticket.")
    if not width or not height or width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE or width * height > MAX_IMAGE_PIXELS:
        raise ReceiptError("La resolución es demasiado grande o no es válida. Mandá una foto más pequeña.")
    return width, height, suffix


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text).lower() if not unicodedata.combining(c))


_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[.,\u00a0 ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\w.,])")
_TOTAL = re.compile(r"^\s*(?:importe\s+total|grand\s+total|amount\s+due|total\s+due|valor\s+total|total(?:\s+(?:a\s+pagar|general|final|compra))?|a\s+pagar)\b\s*[:=]?", re.I)
_NOT_TOTAL = re.compile(r"\b(?:sub\s*total|subtotal|iva|tax|impuesto|descuento|ahorro|vuelto|cambio|articulos|items|unidades|cantidad|pagado|pago|efectivo|tarjeta|cuotas|neto|retencion|percepcion)\b")
_META = re.compile(r"\b(?:cuit|cuil|cufe|rut|ruc|ticket|factura|comprobante|fecha|hora|inicio|cajero|caja|iva|telefono|tel|direccion|domicilio|cliente|consumidor|subtotal|total|efectivo|tarjeta|vuelto|cambio|impuesto|gracias|pago|descuento|ahorro|retencion|percepcion)\b")


def _decimal(text: str) -> Decimal | None:
    compact = text.replace(" ", "").replace("\u00a0", "")
    try:
        if "," in compact and "." in compact:
            decimal_mark = "," if compact.rfind(",") > compact.rfind(".") else "."
            other_mark = "." if decimal_mark == "," else ","
            if len(compact.rsplit(decimal_mark, 1)[1]) not in (1, 2):
                return None
            compact = compact.replace(other_mark, "").replace(decimal_mark, ".")
        elif "," in compact or "." in compact:
            mark = "," if "," in compact else "."
            groups = compact.split(mark)
            if len(groups) == 2 and len(groups[-1]) in (1, 2):
                compact = compact.replace(mark, ".")
            elif all(len(part) == 3 for part in groups[1:]):
                compact = "".join(groups)
            else:
                return None
        value = Decimal(compact)
        if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
            return None
        return value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _amounts(text: str) -> list[Decimal]:
    matches = list(_NUMBER.finditer(text))
    values = []
    for match in matches:
        # A refund must never be silently converted to a positive expense.
        prefix = text[:match.start()].rstrip()
        if re.search(r"[-−(]\s*(?:(?:u\s*\$[sd]?|r\s*\$|ar\s*\$|usd|ars|eur|brl|[$€])\s*)?$", prefix, re.I):
            continue
        value = _decimal(match.group())
        if value is not None:
            values.append(value)
    return values


def _currencies(text: str) -> set[str]:
    upper = _fold(text).upper()
    currencies = set()
    for code, pattern in (
        ("USD", r"\bUSD\b|U\s*\$\s*[SD]?|\bDOLARES?\b|\bUS\s*DOLLARS?\b"),
        ("ARS", r"\bARS\b|AR\s*\$|\bPESOS?\b"),
        ("BRL", r"\bBRL\b|R\s*\$|\bREAIS\b|\bREAL\b"),
        ("EUR", r"\bEUR\b|€|\bEUROS?\b"),
    ):
        if re.search(pattern, upper):
            currencies.add(code)
    return currencies


def _extract_date(lines: list[str], today: date | None) -> date | None:
    dates = []
    for line in lines:
        if re.search(r"\b(venc|vencimiento|expiry|validade)\b", _fold(line)):
            continue
        for match in re.finditer(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4})\b", line):
            token = match.group()
            formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")
            for fmt in formats:
                try:
                    value = datetime.strptime(token, fmt).date()
                except ValueError:
                    continue
                if today is None or value <= today:
                    dates.append(value)
                break
    unique = set(dates)
    return next(iter(unique)) if len(unique) == 1 else None


def parse_receipt_text(text: str, *, caption: str | None = None, today: date | None = None) -> ReceiptDraft:
    """Turn OCR output into a conservative draft, still requiring confirmation."""
    if not isinstance(text, str) or not text.strip():
        raise ReceiptError("No pude leer texto en la foto. Probá con una foto nítida y de frente.")
    if len(text.encode("utf-8")) > MAX_OCR_BYTES:
        raise ReceiptError("El ticket tiene demasiado texto. Mandá una foto solo del comprobante.")
    lines = [" ".join(line.split())[:500] for line in text.splitlines() if line.strip()]
    raw_text = "\n".join(lines)
    warnings: list[str] = []
    candidates: list[tuple[int, Decimal, str]] = []
    first_total = len(lines)
    for index, line in enumerate(lines):
        folded = _fold(line)
        total = _TOTAL.match(folded)
        if total is None or _NOT_TOTAL.search(folded):
            continue
        first_total = min(first_total, index)
        # Strip exactly the matched label, preserving the original text's
        # amount punctuation. NFKD folding only changes accents, not widths
        # of ordinary ASCII labels used by the matcher.
        tail = folded[total.end():]
        values = _amounts(tail)
        candidate_line = line
        if not values and not tail.strip(" :=$") and index + 1 < len(lines):
            following = lines[index + 1]
            if not re.search(r"[A-Za-z]", following.replace("ARS", "").replace("USD", "").replace("BRL", "").replace("EUR", "")):
                values = _amounts(following)
                candidate_line += " " + following
        if len(values) == 1:
            priority = 2 if re.search(r"(?:a pagar|general|final|grand|due|importe total|valor total)", total.group()) else 1
            candidates.append((priority, values[0], candidate_line))
    amount = None
    candidate_currency: set[str] = set()
    if candidates:
        priority = max(candidate[0] for candidate in candidates)
        best = [candidate for candidate in candidates if candidate[0] == priority]
        amounts = {candidate[1] for candidate in best}
        if len(amounts) == 1:
            amount = next(iter(amounts))
            for _, _, line in best:
                candidate_currency.update(_currencies(line))
        else:
            warnings.append("Encontré más de un total distinto; indicá el importe correcto.")
    if amount is None and not warnings:
        warnings.append("No pude identificar un total único; indicá el importe antes de guardar.")
    currencies = candidate_currency or _currencies(raw_text)
    if len(currencies) > 1:
        warnings.append("El ticket menciona varias monedas; confirmá cuál corresponde al total.")
        amount = None
    currency = next(iter(currencies)) if len(currencies) == 1 else "ARS"
    expense_date = _extract_date(lines, today)
    if expense_date is None:
        warnings.append("No pude identificar una fecha única; confirmá la fecha del gasto.")
    merchant = "Ticket"
    for line in lines[:6]:
        if len(line) >= 3 and any(c.isalpha() for c in line) and not _META.search(_fold(line)) and not re.search(r"\d{5,}", line):
            merchant = line[:120]
            break
    if caption and caption.strip():
        merchant = " ".join(caption.split())[:120]
    items = []
    for line in lines[:first_total]:
        if _META.search(_fold(line)):
            continue
        tokens = list(_NUMBER.finditer(line))
        if not tokens:
            continue
        item_amount = _decimal(tokens[-1].group())
        name = line[:tokens[-1].start()].strip(" :$€-")
        if item_amount is not None and len(name) >= 2 and any(c.isalpha() for c in name):
            items.append(ReceiptItem(name=name[:120], amount=item_amount))
            if len(items) >= 25:
                break
    return ReceiptDraft(name=merchant, amount=amount, currency=currency,
                        expense_date=expense_date, items=tuple(items), warnings=tuple(warnings), raw_text=raw_text)


class ReceiptService:
    def __init__(self, *, timeout: float = 40.0) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("OCR timeout must be between 0 and 60 seconds")
        self.timeout = timeout
        self._slots = asyncio.Semaphore(1)

    async def analyze_receipt(
        self, image: bytes, caption: str | None = None, *, today: date | None = None,
    ) -> ReceiptDraft:
        _, _, suffix = image_dimensions(image)
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="tge-receipt-") as directory:
                source = Path(directory) / ("receipt" + suffix)
                destination = Path(directory) / "ocr"
                source.write_bytes(image)
                try:
                    await run_local_process(
                        ["tesseract", str(source), str(destination), "-l", "spa+eng+por", "--psm", "6"],
                        timeout=self.timeout,
                    )
                except FileNotFoundError as exc:
                    raise ReceiptUnavailable("El lector local de tickets no está instalado. Podés cargar el gasto por texto.") from exc
                except asyncio.TimeoutError as exc:
                    raise ReceiptUnavailable("La lectura del ticket tardó demasiado. Probá una foto más pequeña o cargalo por texto.") from exc
                except ProcessFailure as exc:
                    raise ReceiptError("No pude leer esa imagen. Probá una foto nítida JPEG o PNG.") from exc
                output = destination.with_suffix(".txt")
                if not output.is_file() or output.stat().st_size > MAX_OCR_BYTES:
                    raise ReceiptError("No pude obtener un texto de ticket válido. Probá otra foto.")
                text = output.read_text(encoding="utf-8", errors="replace")
            return parse_receipt_text(text, caption=caption, today=today)
