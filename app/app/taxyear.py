"""UK tax year helpers (6 Apr → 5 Apr inclusive)."""
import re
from datetime import date


def tax_year_for(d: date) -> str:
    start = d.year if d >= date(d.year, 4, 6) else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def tax_year_bounds(ty: str) -> tuple[date, date]:
    start_year = int(parse_tax_year(ty).split("-")[0])
    return date(start_year, 4, 6), date(start_year + 1, 4, 5)


def current_tax_year() -> str:
    return tax_year_for(date.today())


def parse_tax_year(s: str) -> str:
    s = s.strip()
    m = re.fullmatch(r"(\d{4})[-/](\d{2})", s)
    if m:
        start, end_short = int(m.group(1)), int(m.group(2))
        if end_short != (start + 1) % 100:
            raise ValueError(f"invalid tax year: {s}")
        return f"{start}-{end_short:02d}"
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        start = int(m.group(1))
        return f"{start}-{(start + 1) % 100:02d}"
    raise ValueError(f"invalid tax year: {s}")
