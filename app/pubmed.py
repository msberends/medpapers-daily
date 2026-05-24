"""Shared PubMed utilities used by fetch.py and app/routes/staff.py."""
import csv
import html as _html
import json
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

BASE_DIR = Path(__file__).parent.parent  # /var/www/medpapers-daily


def _norm_issn(issn: str) -> str:
    return issn.replace("-", "").strip().upper()


def _text(el, path: str, default: str = "") -> str:
    node = el.find(path)
    return _html.unescape(node.text.strip()) if node is not None and node.text else default


def _html_text(el) -> str:
    """Return element text with <i> children converted to <em>, all text HTML-escaped."""
    if el is None:
        return ""
    parts = []
    if el.text:
        parts.append(_html.escape(_html.unescape(el.text)))
    for child in el:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        inner = _html.escape(_html.unescape("".join(child.itertext())))
        parts.append(f"<em>{inner}</em>" if tag == "i" else inner)
        if child.tail:
            parts.append(_html.escape(_html.unescape(child.tail)))
    return "".join(parts).strip()


def search_pubmed(query: str, mindate: str, maxdate: str,
                  api_key: str, retmax: int = 200) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "mindate": mindate,
        "maxdate": maxdate,
        "datetype": "edat",
        "retmax": retmax,
        "retmode": "json",
        "api_key": api_key,
    }
    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params=params, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def fetch_pubmed_records(pmids: list[str], api_key: str) -> ET.Element:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
        "api_key": api_key,
    }
    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=params, timeout=60,
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def parse_article(article_set_child: ET.Element) -> dict | None:
    """Parse a single PubmedArticle XML element into a dict.

    The returned dict includes 'author_orcids': a list parallel to 'authors'
    where each entry is an ORCID string or None.  This key is not stored in
    the papers table but is used by the staff-publications workflow.
    """
    medline = article_set_child.find(".//MedlineCitation")
    if medline is None:
        return None
    pmid_el = medline.find("PMID")
    if pmid_el is None or not pmid_el.text:
        return None
    pmid = pmid_el.text.strip()
    art = medline.find("Article")
    if art is None:
        return None

    title = _html_text(art.find("ArticleTitle"))
    abstract_sections = []
    for node in art.findall(".//AbstractText"):
        text = _html.unescape("".join(node.itertext()).strip())
        if not text:
            continue
        raw_label = (node.get("Label") or "").strip()
        label = raw_label.title() if raw_label else ""
        abstract_sections.append({"label": label, "text": text})
    abstract = " ".join(s["text"] for s in abstract_sections)
    has_labels = any(s["label"] for s in abstract_sections)
    abstract_structured = json.dumps(abstract_sections) if has_labels else None

    authors: list[str] = []
    author_orcids: list[str | None] = []
    author_affil_raw: list[list[str]] = []
    for auth in art.findall(".//Author"):
        ln = _text(auth, "LastName")
        fn = _text(auth, "ForeName") or _text(auth, "Initials")
        affils = [
            _html.unescape(el.text.strip())
            for el in auth.findall("AffiliationInfo/Affiliation")
            if el.text
        ]
        orcid: str | None = None
        for id_el in auth.findall("Identifier"):
            if id_el.get("Source") == "ORCID" and id_el.text:
                orcid = id_el.text.strip()
                break
        if ln:
            authors.append(f"{ln}, {fn}".strip(", "))
            author_orcids.append(orcid)
            author_affil_raw.append(affils)
        else:
            cn = _text(auth, "CollectiveName")
            if cn:
                authors.append(cn)
                author_orcids.append(orcid)
                author_affil_raw.append(affils)

    aff_list: list[str] = []
    aff_index: dict[str, int] = {}
    for affils in author_affil_raw:
        for a in affils:
            if a not in aff_index:
                aff_index[a] = len(aff_list)
                aff_list.append(a)
    author_aff = [[aff_index[a] for a in affils] for affils in author_affil_raw]
    affiliations = json.dumps({"aff_list": aff_list, "author_aff": author_aff}) if aff_list else None

    journal_el = art.find("Journal")
    journal_name = iso_abbreviation = issn = ""
    if journal_el is not None:
        iso_abbreviation = _text(journal_el, "ISOAbbreviation") or ""
        journal_name = _text(journal_el, "Title") or iso_abbreviation
        issn_el = journal_el.find("ISSN[@IssnType='Print']")
        if issn_el is None:
            issn_el = journal_el.find("ISSN")
        if issn_el is not None and issn_el.text:
            issn = issn_el.text.strip()

    pub_date = ""
    pub_date_el = art.find(".//PubDate")
    if pub_date_el is not None:
        year = _text(pub_date_el, "Year")
        month = _text(pub_date_el, "Month")
        day = _text(pub_date_el, "Day")
        med_date = _text(pub_date_el, "MedlineDate")
        if year:
            pub_date = f"{year}-{month[:3] if month else ''}-{day}".strip("-")
        elif med_date:
            pub_date = med_date

    epub_date = ""
    for date_el in art.findall(".//ArticleDate"):
        if date_el.get("DateType") == "Electronic":
            y = _text(date_el, "Year")
            m = _text(date_el, "Month")
            d = _text(date_el, "Day")
            if y and m and d:
                epub_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            break

    doi = ""
    for id_el in article_set_child.findall(".//ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = (id_el.text or "").strip()
            break

    mesh_terms = [
        _html.unescape(node.text.strip())
        for node in medline.findall(".//MeshHeading/DescriptorName")
        if node.text
    ]
    keywords = [
        _html.unescape(node.text.strip())
        for node in medline.findall(".//KeywordList/Keyword")
        if node.text
    ]

    return {
        "pmid": pmid,
        "title": title,
        "authors": json.dumps(authors),
        "author_orcids": author_orcids,  # list[str|None], not stored in papers table
        "affiliations": affiliations,
        "journal": journal_name,
        "iso_abbreviation": iso_abbreviation or None,
        "issn": issn,
        "pub_date": pub_date,
        "epub_date": epub_date,
        "abstract": abstract,
        "abstract_structured": abstract_structured,
        "doi": doi,
        "mesh_terms": json.dumps(mesh_terms),
        "keywords": json.dumps(keywords),
    }


def upsert_paper(conn, record: dict, scopus_mapping: dict | None = None) -> bool:
    """Insert a paper if it doesn't exist; update metadata if it does.

    Applies Scopus quartile data from *scopus_mapping* when available.
    Returns True if the paper was newly inserted.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    issn = record.get("issn") or ""
    scopus_data = scopus_mapping.get(_norm_issn(issn)) if (scopus_mapping and issn) else None
    quartile = citescore = percentile = publisher = scopus_title = None
    if scopus_data:
        quartile, citescore, percentile, publisher, scopus_title = scopus_data

    existing = conn.execute(
        "SELECT pmid, scopus_quartile FROM papers WHERE pmid = ?", (record["pmid"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO papers
               (pmid, title, authors, affiliations, journal, iso_abbreviation, issn,
                pub_date, epub_date, abstract, abstract_structured, doi,
                oa_url, mesh_terms, keywords,
                scopus_quartile, scopus_citescore, scopus_percentile, publisher, first_seen_at)
               VALUES
               (:pmid,:title,:authors,:affiliations,:journal,:iso_abbreviation,:issn,
                :pub_date,:epub_date,:abstract,:abstract_structured,:doi,
                :oa_url,:mesh_terms,:keywords,
                :scopus_quartile,:scopus_citescore,:scopus_percentile,:publisher,:first_seen_at)""",
            {
                **record,
                "oa_url": None,
                "scopus_quartile": quartile,
                "scopus_citescore": citescore,
                "scopus_percentile": percentile,
                "publisher": publisher,
                "journal": scopus_title or record.get("journal", ""),
                "first_seen_at": now_iso,
            },
        )
        return True
    else:
        conn.execute(
            """UPDATE papers SET mesh_terms=?, keywords=?, affiliations=?,
               abstract=?, abstract_structured=? WHERE pmid=?""",
            (record["mesh_terms"], record["keywords"], record["affiliations"],
             record["abstract"], record["abstract_structured"], record["pmid"]),
        )
        if quartile and not existing["scopus_quartile"]:
            conn.execute(
                """UPDATE papers SET scopus_quartile=?, scopus_citescore=?,
                   scopus_percentile=?, publisher=COALESCE(publisher, ?) WHERE pmid=?""",
                (quartile, citescore, percentile, publisher, record["pmid"]),
            )
        return False


def computed_author_pmids(seed_pmid: str, last_name: str, initials: str,
                          api_key: str = "") -> list[str]:
    """Return PMIDs for a disambiguated author using PubMed's cauthor_id parameter.

    This replicates exactly what the PubMed web interface does when you click an
    author name: esearch with term="{last} {initials}[au]" and cauthor_id={seed}.
    """
    params = {
        "db": "pubmed",
        "term": f"{last_name} {initials}[au]",
        "cauthor_id": seed_pmid,
        "retmax": 500,
        "retmode": "json",
    }
    if api_key:
        params["api_key"] = api_key
    try:
        r = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=params, timeout=30,
        )
        r.raise_for_status()
        return r.json().get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []


def load_scopus_mapping(base_dir: Path) -> dict[str, tuple]:
    """Load ISSN → (quartile, citescore, percentile, publisher, title) from the Scopus CSVs."""
    mapping: dict[str, tuple] = {}
    for csv_path in (
        base_dir / "data" / "scopus_journals.csv",
        base_dir / "data" / "scopus_extended.csv",
    ):
        if not csv_path.exists():
            continue
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    q_num = int(float(row.get("quartile") or ""))
                    quartile = f"Q{q_num}" if 1 <= q_num <= 4 else None
                except (ValueError, TypeError):
                    quartile = None
                if quartile is None:
                    continue

                def _pf(raw: str) -> float | None:
                    raw = (raw or "").strip()
                    try:
                        return float(raw.replace(",", ".")) if raw else None
                    except ValueError:
                        return None

                entry = (
                    quartile,
                    _pf(row.get("citescore")),
                    _pf(row.get("percentile")),
                    (row.get("publisher") or "").strip() or None,
                    (row.get("title") or "").strip() or None,
                )
                for issn_field in (row.get("issn") or "", row.get("eIssn") or ""):
                    for raw_issn in issn_field.split(","):
                        raw_issn = raw_issn.strip()
                        if raw_issn:
                            norm = _norm_issn(raw_issn)
                            if norm not in mapping:
                                mapping[norm] = entry
    return mapping
