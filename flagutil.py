"""Shared country-extraction utilities used by both app/flags.py and email_digest.py."""
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

# ISO 3166-1 alpha-2 → canonical display name
_ISO_TO_NAME: dict[str, str] = {
    # Americas
    "us": "United States", "ca": "Canada", "mx": "Mexico",
    "br": "Brazil", "ar": "Argentina", "cl": "Chile",
    "co": "Colombia", "pe": "Peru", "ve": "Venezuela",
    "bo": "Bolivia", "ec": "Ecuador", "py": "Paraguay",
    "uy": "Uruguay", "cu": "Cuba", "ht": "Haiti",
    "do": "Dominican Republic", "jm": "Jamaica",
    "tt": "Trinidad and Tobago", "bb": "Barbados",
    "pa": "Panama", "cr": "Costa Rica", "gt": "Guatemala",
    "hn": "Honduras", "sv": "El Salvador", "ni": "Nicaragua",
    "bz": "Belize", "gy": "Guyana", "sr": "Suriname",
    # Europe
    "gb": "United Kingdom", "ie": "Ireland", "de": "Germany",
    "fr": "France", "it": "Italy", "es": "Spain", "pt": "Portugal",
    "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland",
    "at": "Austria", "se": "Sweden", "no": "Norway", "dk": "Denmark",
    "fi": "Finland", "is": "Iceland", "lu": "Luxembourg",
    "mt": "Malta", "cy": "Cyprus", "gr": "Greece", "pl": "Poland",
    "cz": "Czech Republic", "sk": "Slovakia", "hu": "Hungary",
    "ro": "Romania", "bg": "Bulgaria", "hr": "Croatia",
    "rs": "Serbia", "si": "Slovenia", "ba": "Bosnia and Herzegovina",
    "mk": "North Macedonia", "al": "Albania", "me": "Montenegro",
    "md": "Moldova", "ua": "Ukraine", "by": "Belarus",
    "ru": "Russia", "ee": "Estonia", "lv": "Latvia",
    "lt": "Lithuania", "xk": "Kosovo",
    # Asia
    "cn": "China", "hk": "Hong Kong", "tw": "Taiwan",
    "jp": "Japan", "kr": "South Korea", "kp": "North Korea",
    "in": "India", "pk": "Pakistan", "bd": "Bangladesh",
    "lk": "Sri Lanka", "np": "Nepal", "bt": "Bhutan",
    "mv": "Maldives", "af": "Afghanistan", "ir": "Iran",
    "iq": "Iraq", "sa": "Saudi Arabia",
    "ae": "United Arab Emirates", "qa": "Qatar",
    "kw": "Kuwait", "bh": "Bahrain", "om": "Oman",
    "ye": "Yemen", "il": "Israel", "ps": "Palestine",
    "jo": "Jordan", "lb": "Lebanon", "sy": "Syria",
    "tr": "Turkey", "sg": "Singapore", "my": "Malaysia",
    "id": "Indonesia", "ph": "Philippines", "th": "Thailand",
    "vn": "Vietnam", "kh": "Cambodia", "mm": "Myanmar",
    "la": "Laos", "mn": "Mongolia", "uz": "Uzbekistan",
    "kz": "Kazakhstan", "kg": "Kyrgyzstan", "tj": "Tajikistan",
    "tm": "Turkmenistan", "ge": "Georgia", "am": "Armenia",
    "az": "Azerbaijan",
    # Oceania
    "au": "Australia", "nz": "New Zealand",
    "pg": "Papua New Guinea", "fj": "Fiji",
    # Africa
    "za": "South Africa", "ng": "Nigeria", "ke": "Kenya",
    "et": "Ethiopia", "gh": "Ghana", "tz": "Tanzania",
    "ug": "Uganda", "rw": "Rwanda", "zm": "Zambia",
    "zw": "Zimbabwe", "na": "Namibia", "bw": "Botswana",
    "mz": "Mozambique", "mw": "Malawi", "mg": "Madagascar",
    "ls": "Lesotho", "sz": "Eswatini", "ao": "Angola",
    "cm": "Cameroon", "ci": "Ivory Coast", "sn": "Senegal",
    "ml": "Mali", "bf": "Burkina Faso", "gn": "Guinea",
    "sl": "Sierra Leone", "lr": "Liberia", "tg": "Togo",
    "bj": "Benin", "td": "Chad", "ne": "Niger",
    "mr": "Mauritania", "dj": "Djibouti", "er": "Eritrea",
    "so": "Somalia", "sd": "Sudan", "ss": "South Sudan",
    "eg": "Egypt", "ly": "Libya", "tn": "Tunisia",
    "dz": "Algeria", "ma": "Morocco",
    "cd": "DR Congo", "cg": "Republic of Congo",
    "ga": "Gabon", "gq": "Equatorial Guinea",
    "cf": "Central African Republic",
    "cv": "Cape Verde", "mu": "Mauritius", "sc": "Seychelles",
    "km": "Comoros", "st": "São Tomé and Príncipe",
    "gm": "Gambia", "gw": "Guinea-Bissau",
}

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
        # Token may bundle state/country with a zip code (e.g. "MA 02115 USA",
        # "VA USA"); scan its space-separated words right-to-left.
        subwords = token.split()
        if len(subwords) > 1:
            for subword in reversed(subwords):
                sk = subword.lower()
                if sk in _COUNTRY_MAP:
                    return _COUNTRY_MAP[sk]
                if sk in _US_STATES:
                    return "us"
    return None


def _extract_country(aff_text: str) -> tuple[str, str] | None:
    """Return (iso, display_name) for the country in a PubMed affiliation string."""
    if not aff_text:
        return None
    s = aff_text.strip()
    s = _ELEC_ADDR_RE.sub('', s)
    s = _EMAIL_RE.sub('', s)

    iso = None
    for segment in s.split(';'):
        seg = segment.strip().rstrip('.')
        parts = [p.strip().rstrip('.') for p in seg.split(',')]
        parts = [p for p in parts if p]
        iso = _scan_parts(parts)
        if iso:
            break

    if not iso:
        dot_parts = [p.strip() for p in s.rstrip('.').split('.')]
        dot_parts = [p for p in dot_parts if p]
        iso = _scan_parts(dot_parts)

    if not iso:
        return None
    return iso, _ISO_TO_NAME.get(iso, iso.upper())


def extract_country(aff_text: str) -> tuple[str, str] | None:
    """Return (iso, display_name) for the country in a PubMed affiliation string."""
    return _extract_country(aff_text)


def extract_country_iso(aff_text: str) -> str | None:
    """Return ISO 3166-1 alpha-2 code for the country in a PubMed affiliation string."""
    result = _extract_country(aff_text)
    return result[0] if result else None
