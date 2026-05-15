"""Country flag utilities: extract country from PubMed affiliation text → ISO code → SVG img tag."""
import re

# Normalised (lowercase) country name / abbreviation → ISO 3166-1 alpha-2
_COUNTRY_MAP: dict[str, str] = {
    # -- Americas --
    "usa": "us", "u.s.a": "us", "u.s": "us",
    "united states": "us", "united states of america": "us",
    "canada": "ca",
    "mexico": "mx",
    "brazil": "br", "brasil": "br",
    "argentina": "ar",
    "chile": "cl",
    "colombia": "co",
    "peru": "pe",
    "venezuela": "ve",
    "bolivia": "bo",
    "ecuador": "ec",
    "paraguay": "py",
    "uruguay": "uy",
    "cuba": "cu",
    "haiti": "ht",
    "dominican republic": "do",
    "jamaica": "jm",
    "trinidad and tobago": "tt",
    "barbados": "bb",
    "panama": "pa",
    "costa rica": "cr",
    "guatemala": "gt",
    "honduras": "hn",
    "el salvador": "sv",
    "nicaragua": "ni",
    "belize": "bz",
    "guyana": "gy",
    "suriname": "sr",
    # -- Europe --
    "uk": "gb", "u.k": "gb",
    "united kingdom": "gb", "great britain": "gb",
    "england": "gb", "scotland": "gb", "wales": "gb", "northern ireland": "gb",
    "ireland": "ie",
    "germany": "de",
    "france": "fr",
    "italy": "it",
    "spain": "es",
    "portugal": "pt",
    "netherlands": "nl", "the netherlands": "nl",
    "belgium": "be",
    "switzerland": "ch",
    "austria": "at",
    "sweden": "se",
    "norway": "no",
    "denmark": "dk",
    "finland": "fi",
    "iceland": "is",
    "luxembourg": "lu",
    "malta": "mt",
    "cyprus": "cy",
    "greece": "gr",
    "poland": "pl",
    "czech republic": "cz", "czechia": "cz",
    "slovakia": "sk",
    "hungary": "hu",
    "romania": "ro",
    "bulgaria": "bg",
    "croatia": "hr",
    "serbia": "rs",
    "slovenia": "si",
    "bosnia and herzegovina": "ba",
    "north macedonia": "mk",
    "albania": "al",
    "montenegro": "me",
    "moldova": "md",
    "ukraine": "ua",
    "belarus": "by",
    "russia": "ru", "russian federation": "ru",
    "estonia": "ee",
    "latvia": "lv",
    "lithuania": "lt",
    "kosovo": "xk",
    # -- Asia --
    "china": "cn",
    "people's republic of china": "cn", "p.r. china": "cn",
    "p. r. china": "cn", "pr china": "cn", "p.r china": "cn",
    "hong kong": "hk",
    "taiwan": "tw", "taiwan, republic of china": "tw",
    "japan": "jp",
    "south korea": "kr", "republic of korea": "kr", "korea": "kr",
    "north korea": "kp",
    "india": "in",
    "pakistan": "pk",
    "bangladesh": "bd",
    "sri lanka": "lk",
    "nepal": "np",
    "bhutan": "bt",
    "maldives": "mv",
    "afghanistan": "af",
    "iran": "ir", "iran (islamic republic of)": "ir",
    "iraq": "iq",
    "saudi arabia": "sa",
    "united arab emirates": "ae", "uae": "ae",
    "qatar": "qa",
    "kuwait": "kw",
    "bahrain": "bh",
    "oman": "om",
    "yemen": "ye",
    "israel": "il",
    "palestine": "ps",
    "jordan": "jo",
    "lebanon": "lb",
    "syria": "sy",
    "turkey": "tr", "türkiye": "tr",
    "singapore": "sg",
    "malaysia": "my",
    "indonesia": "id",
    "philippines": "ph",
    "thailand": "th",
    "vietnam": "vn", "viet nam": "vn",
    "cambodia": "kh",
    "myanmar": "mm",
    "laos": "la",
    "mongolia": "mn",
    "uzbekistan": "uz",
    "kazakhstan": "kz",
    "kyrgyzstan": "kg",
    "tajikistan": "tj",
    "turkmenistan": "tm",
    "georgia": "ge",
    "armenia": "am",
    "azerbaijan": "az",
    # -- Oceania --
    "australia": "au",
    "new zealand": "nz",
    "papua new guinea": "pg",
    "fiji": "fj",
    # -- Africa --
    "south africa": "za",
    "nigeria": "ng",
    "kenya": "ke",
    "ethiopia": "et",
    "ghana": "gh",
    "tanzania": "tz",
    "uganda": "ug",
    "rwanda": "rw",
    "zambia": "zm",
    "zimbabwe": "zw",
    "namibia": "na",
    "botswana": "bw",
    "mozambique": "mz",
    "malawi": "mw",
    "madagascar": "mg",
    "lesotho": "ls",
    "eswatini": "sz", "swaziland": "sz",
    "angola": "ao",
    "cameroon": "cm",
    "ivory coast": "ci", "côte d'ivoire": "ci", "cote d'ivoire": "ci",
    "senegal": "sn",
    "mali": "ml",
    "burkina faso": "bf",
    "guinea": "gn",
    "sierra leone": "sl",
    "liberia": "lr",
    "togo": "tg",
    "benin": "bj",
    "chad": "td",
    "niger": "ne",
    "mauritania": "mr",
    "djibouti": "dj",
    "eritrea": "er",
    "somalia": "so",
    "sudan": "sd",
    "south sudan": "ss",
    "egypt": "eg",
    "libya": "ly",
    "tunisia": "tn",
    "algeria": "dz",
    "morocco": "ma",
    "democratic republic of congo": "cd", "dr congo": "cd", "drc": "cd",
    "congo": "cg", "republic of the congo": "cg",
    "gabon": "ga",
    "equatorial guinea": "gq",
    "central african republic": "cf",
    "cabo verde": "cv", "cape verde": "cv",
    "mauritius": "mu",
    "seychelles": "sc",
    "comoros": "km",
    "sao tome and principe": "st",
    "gambia": "gm",
    "guinea-bissau": "gw",
}

# US state names and common abbreviations — map these to "us"
_US_STATES: frozenset[str] = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california",
    "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
    # two-letter abbreviations
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
    # abbreviated forms seen in PubMed data
    "n.y", "ind",
})

_EMAIL_RE = re.compile(r'[\s.]+\S+@\S+\.[a-zA-Z]{2,}\.?\s*$')
_ELEC_ADDR_RE = re.compile(r'[. ]*[Ee]lectronic\s+address:.*$')
# Domain-like token: contains a dot but no space (e.g. "rsu.lv", "upc.edu.pe")
_DOMAIN_RE = re.compile(r'^[\w-]+\.[\w.-]+$')


def _scan_parts(parts: list[str]) -> str | None:
    """Walk up to the last 4 tokens looking for a known country or US state."""
    for token in reversed(parts[-4:]):
        key = token.lower()
        # Country map is checked BEFORE the domain test so that abbreviated
        # codes like "U.K" or "N.Y" are not mistakenly treated as domain names.
        if key in _COUNTRY_MAP:
            return _COUNTRY_MAP[key]
        if key in _US_STATES:
            return "us"
        # Skip domain-like tokens (e.g. "rsu.lv", "upc.edu.pe")
        if _DOMAIN_RE.match(token):
            continue
    return None


def extract_country_iso(aff_text: str) -> str | None:
    """Return ISO 3166-1 alpha-2 code for the country in a PubMed affiliation string."""
    if not aff_text:
        return None
    s = aff_text.strip()
    # Strip "Electronic address: …" and bare email suffixes
    s = _ELEC_ADDR_RE.sub('', s)
    s = _EMAIL_RE.sub('', s)

    # Some affiliation strings concatenate multiple sub-affiliations with "; ".
    # Try each segment so the country in any sub-affiliation can be found.
    for segment in s.split(';'):
        seg = segment.strip().rstrip('.')
        parts = [p.strip().rstrip('.') for p in seg.split(',')]
        parts = [p for p in parts if p]
        iso = _scan_parts(parts)
        if iso:
            return iso

    # Fallback: some affiliations (e.g. certain Brazilian institutions) use
    # periods as the field separator instead of commas.
    dot_parts = [p.strip() for p in s.rstrip('.').split('.')]
    dot_parts = [p for p in dot_parts if p]
    return _scan_parts(dot_parts)


def affil_flag_html(aff_text: str) -> str:
    """Return an <img> tag for the country flag, or a fixed-width placeholder."""
    iso = extract_country_iso(aff_text or '')
    if not iso:
        return '<span style="display:inline-block;width:20px;height:14px;margin-right:.3em"></span>'
    return (
        f'<img src="/static/flags/{iso}.svg" width="20" height="14" alt="{iso.upper()}" '
        f'style="vertical-align:baseline;margin-right:.3em;flex-shrink:0">'
    )
