"""CSV statement parsing and validation. Pure functions: nothing here touches the database."""
import csv
import hashlib
import io
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

from apps.core.errors import DomainError
from apps.core.money import money

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
DATE_FORMATS = {"iso": "%Y-%m-%d", "dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y"}
HEADERS = {
    "date": {"date", "transaction date", "posting date", "value date"},
    "description": {"description", "details", "narration", "memo", "particulars"},
    "reference": {"reference", "ref", "cheque no", "check no", "transaction id"},
    "amount": {"amount"},
    "debit": {"debit", "withdrawal", "withdrawals", "paid out", "money out"},
    "credit": {"credit", "deposit", "deposits", "paid in", "money in"},
}


def parse_amount(text):
    t = (text or "").strip().replace(",", "").replace(" ", "")
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a number")
    if not v.is_finite():
        raise ValueError(f"'{text}' is not a number")
    return money(-v if neg else v)


def _columns(header):
    found = {}
    for i, h in enumerate(header):
        key = re.sub(r"\s+", " ", h.strip().lower())
        for name, aliases in HEADERS.items():
            if key in aliases and name not in found:
                found[name] = i
    if "date" not in found or not ("amount" in found or ("debit" in found and "credit" in found)):
        raise DomainError("The CSV needs a header row with a Date column and either Amount or both Debit and Credit columns.", "csv_headers")
    return found


def parse_statement(raw, date_format="iso"):
    """Returns (rows, errors). rows: dicts {row, date, description, reference, amount}. Any error means nothing may be imported."""
    if len(raw) > MAX_BYTES:
        raise DomainError("The file is larger than 2 MB.", "file_too_large")
    if date_format not in DATE_FORMATS:
        raise DomainError("date_format must be one of: iso, dmy, mdy.", "invalid_date_format")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DomainError("The file must be UTF-8 text (CSV).", "csv_encoding")
    if "\x00" in text:
        raise DomainError("The file is not a CSV text file.", "csv_encoding")
    reader = csv.reader(io.StringIO(text))
    rows_in = [r for r in reader if any(c.strip() for c in r)]
    if not rows_in:
        raise DomainError("The file is empty.", "csv_empty")
    cols = _columns(rows_in[0])
    body = rows_in[1:]
    if not body:
        raise DomainError("The file has a header but no transactions.", "csv_empty")
    if len(body) > MAX_ROWS:
        raise DomainError(f"The file has more than {MAX_ROWS} rows; import it in smaller periods.", "too_many_rows")
    fmt, rows, errors = DATE_FORMATS[date_format], [], []
    get = lambda r, k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else ""
    for n, r in enumerate(body, start=2):  # row numbers as the user sees them in a spreadsheet
        try:
            d = datetime.strptime(get(r, "date"), fmt).date()
        except ValueError:
            errors.append({"row": n, "error": f"Date '{get(r, 'date')}' does not match the {date_format} format"})
            continue
        try:
            if "amount" in cols:
                amt = parse_amount(get(r, "amount"))
            else:
                dr, cr = parse_amount(get(r, "debit")), parse_amount(get(r, "credit"))
                if dr and cr:
                    raise ValueError("a row cannot have both a debit and a credit")
                amt = (cr or Decimal(0)) - (dr or Decimal(0)) if (dr or cr) else None
        except ValueError as e:
            errors.append({"row": n, "error": str(e)})
            continue
        if amt is None or amt == 0:
            errors.append({"row": n, "error": "Amount is missing or zero"})
            continue
        if abs(amt) >= Decimal("1e15"):
            errors.append({"row": n, "error": "Amount is too large"})
            continue
        rows.append({"row": n, "date": d, "description": get(r, "description")[:300], "reference": get(r, "reference")[:100], "amount": amt})
    return rows, errors


def fingerprints(rows):
    """Adds `fingerprint` to each row. Identical rows inside one file stay distinct (n-th occurrence is part of the key)."""
    seen = Counter()
    for r in rows:
        base = "|".join([r["date"].isoformat(), str(r["amount"]), " ".join(r["description"].lower().split()), r["reference"].lower()])
        seen[base] += 1
        r["fingerprint"] = hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()
    return rows


def file_hash(raw):
    return hashlib.sha256(raw).hexdigest()
