"""Country flag utilities: extract country from PubMed affiliation text → ISO code → SVG img tag."""
from pathlib import Path

from flagutil import (  # noqa: F401 — re-exported for callers that import from here
    _COUNTRY_MAP, _US_STATES, _ISO_TO_NAME,
    extract_country, extract_country_iso,
)

_BORDER_CODES: frozenset[str] = frozenset(
    (Path(__file__).parent.parent / "static" / "flags" / "border-requirement.txt")
    .read_text().split()
)


def affil_flag_html(aff_text: str) -> str:
    """Return an <img> tag for the country flag, or a fixed-width placeholder."""
    result = extract_country(aff_text or '')
    if not result:
        return '<span style="display:inline-block;width:20px;height:14px;margin-right:.3em"></span>'
    iso, name = result
    border = ";outline:1px solid rgba(0,0,0,.1)" if iso in _BORDER_CODES else ""
    return (
        f'<img src="/static/flags/{iso}.svg" width="20" height="14" alt="{iso.upper()}" '
        f'data-bs-toggle="tooltip" data-bs-placement="top" title="{name}" '
        f'style="vertical-align:baseline;margin-right:.3em;flex-shrink:0{border}">'
    )
