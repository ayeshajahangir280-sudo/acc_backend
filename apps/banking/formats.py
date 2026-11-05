"""OFX / QIF statement parsing, producing the same rows as the CSV importer (pure functions, no database access).

`parse_any` picks the parser from the file content (OFX) or extension (QIF); everything else goes to the unchanged CSV importer.
"""
import re
from datetime import datetime
from decimal import Decimal

from apps.core.errors import DomainError

from . import importer

_TAG = re.compile(r"<(STMTTRN)>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|</STMTTRN>|$)", re.S | re.I)


def _text(raw):
    if len(raw) > importer.MAX_BYTES:
        raise DomainError("The file is larger than 2 MB.", "file_too_large")
    if b"\x00" in raw:
        raise DomainError("The file is not a text statement.", "csv_encoding")
    for enc in ("utf-8-sig", "cp1252"):  # OFX 1.x files are very often Windows-1252
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DomainError("The file encoding is not supported.", "csv_encoding")


def is_ofx(raw):
    head = raw[:2000].upper()
    return b"<OFX" in head or b"OFXHEADER" in head


def _field(block, name):
    m = re.search(rf"<{name}>\s*([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def parse_ofx(raw):
    text = _text(raw)
    rows, errors = [], []
    blocks = _TAG.findall(text)
    if not blocks:
        raise DomainError("No transactions were found in the OFX file.", "csv_empty")
    if len(blocks) > importer.MAX_ROWS:
        raise DomainError(f"The file has more than {importer.MAX_ROWS} rows; import it in smaller periods.", "too_many_rows")
    for n, (_, block) in enumerate(blocks, start=1):
        d = _field(block, "DTPOSTED")[:8]
        try:
            day = datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            errors.append({"row": n, "error": f"Date '{d}' is not a valid OFX date"})
            continue
        try:
            amt = importer.parse_amount(_field(block, "TRNAMT"))
        except ValueError as e:
            errors.append({"row": n, "error": str(e)})
            continue
        if amt is None or amt == 0:
            errors.append({"row": n, "error": "Amount is missing or zero"})
            continue
        if abs(amt) >= Decimal("1e15"):
            errors.append({"row": n, "error": "Amount is too large"})
            continue
        desc = " ".join(x for x in (_field(block, "NAME"), _field(block, "MEMO")) if x)
        ref = _field(block, "FITID") or _field(block, "CHECKNUM")
        rows.append({"row": n, "date": day, "description": desc[:300], "reference": ref[:100], "amount": amt})
    return rows, errors


def parse_qif(raw, date_format="mdy"):
    text = _text(raw)
    if date_format not in importer.DATE_FORMATS:
        raise DomainError("date_format must be one of: iso, dmy, mdy.", "invalid_date_format")
    fmt = importer.DATE_FORMATS[date_format]
    rows, errors, rec, n = [], [], {}, 0

    def flush():
        nonlocal rec, n
        if not rec:
            return
        n += 1
        r, rec = rec, {}
        try:
            d = r.get("D", "").replace("'", "/").strip()
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", d):                 # two-digit year, e.g. 3/ 4/26
                d = d[:-2] + "20" + d[-2:]
            d = re.sub(r"\s+", "", d)
            day = datetime.strptime(d, fmt).date()
        except ValueError:
            errors.append({"row": n, "error": f"Date '{r.get('D', '')}' does not match the {date_format} format"})
            return
        try:
            amt = importer.parse_amount(r.get("T", r.get("U", "")))
        except ValueError as e:
            errors.append({"row": n, "error": str(e)})
            return
        if amt is None or amt == 0:
            errors.append({"row": n, "error": "Amount is missing or zero"})
            return
        if abs(amt) >= Decimal("1e15"):
            errors.append({"row": n, "error": "Amount is too large"})
            return
        desc = " ".join(x for x in (r.get("P", ""), r.get("M", "")) if x.strip())
        rows.append({"row": n, "date": day, "description": desc[:300], "reference": r.get("N", "")[:100], "amount": amt})

    for line in text.splitlines():
        if not line.strip():
            continue
        code, val = line[0], line[1:].strip()
        if code == "!":
            continue                                                      # header such as !Type:Bank
        if code == "^":
            flush()
        else:
            rec[code] = val
    flush()
    if not rows and not errors:
        raise DomainError("No transactions were found in the QIF file.", "csv_empty")
    if len(rows) + len(errors) > importer.MAX_ROWS:
        raise DomainError(f"The file has more than {importer.MAX_ROWS} rows; import it in smaller periods.", "too_many_rows")
    return rows, errors


def parse_any(raw, file_name, date_format="iso"):
    name = (file_name or "").lower()
    if is_ofx(raw) or name.endswith((".ofx", ".qfx")):
        return parse_ofx(raw)
    if name.endswith(".qif"):
        return parse_qif(raw, date_format if date_format in importer.DATE_FORMATS else "mdy")
    return importer.parse_statement(raw, date_format)
