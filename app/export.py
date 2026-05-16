import json
import re
from datetime import datetime, timezone

from app.db import conn_ctx


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _wrap_nbib_field(tag: str, value: str) -> list[str]:
    prefix = f"{tag:<4}- "
    cont = "      "
    words = str(value).split()
    if not words:
        return [prefix.rstrip()]
    lines: list[str] = []
    current = prefix
    for word in words:
        if current == prefix:
            current += word
        elif len(current) + 1 + len(word) > 79:
            lines.append(current)
            current = cont + word
        else:
            current += " " + word
    if current.strip():
        lines.append(current)
    return lines


def papers_to_ris(rows: list) -> str:
    lines = []
    for row in rows:
        authors = json.loads(row["authors"] or "[]")
        mesh_terms = json.loads(row["mesh_terms"] or "[]")
        pub_date = row["pub_date"] or ""
        year = pub_date[:4] if pub_date else ""

        lines.append("TY  - JOUR")
        lines.append(f"TI  - {_strip_tags(row['title'])}")
        for author in authors:
            lines.append(f"AU  - {author}")
        lines.append(f"JO  - {row['journal']}")
        if row["issn"]:
            lines.append(f"SN  - {row['issn']}")
        if year:
            lines.append(f"PY  - {year}")
        if pub_date:
            lines.append(f"DA  - {pub_date}")
        if row["abstract"]:
            lines.append(f"AB  - {row['abstract']}")
        if row["doi"]:
            lines.append(f"DO  - {row['doi']}")
        if row["oa_url"]:
            lines.append(f"UR  - {row['oa_url']}")
        else:
            lines.append(f"UR  - https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}")
        lines.append(f"AN  - PubMed:{row['pmid']}")
        for kw in mesh_terms:
            lines.append(f"KW  - {kw}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def papers_to_nbib(rows: list) -> str:
    lines: list[str] = []
    for row in rows:
        authors = json.loads(row["authors"] or "[]")
        mesh_terms = json.loads(row["mesh_terms"] or "[]")
        keywords = json.loads(row["keywords"] or "[]")
        affiliations_raw = json.loads(row["affiliations"] or "null") or {}
        aff_list = affiliations_raw.get("aff_list", [])
        author_aff = affiliations_raw.get("author_aff", [])
        pub_date = row["pub_date"] or ""
        epub_date = row["epub_date"] or ""
        title = _strip_tags(row["title"])

        lines.append(f"PMID- {row['pmid']}")
        lines.append("OWN - NLM")

        if row["issn"]:
            lines.append(f"IS  - {row['issn']}")

        if pub_date:
            lines.append(f"DP  - {pub_date}")

        lines.extend(_wrap_nbib_field("TI", title))

        if row["doi"]:
            lines.append(f"LID - {row['doi']} [doi]")

        if row["abstract"]:
            lines.extend(_wrap_nbib_field("AB", row["abstract"]))

        for i, author in enumerate(authors):
            lines.append(f"FAU - {author}")
            if ", " in author:
                last, rest = author.split(", ", 1)
                initials = "".join(w[0] for w in rest.split())
                au = f"{last} {initials}" if initials else last
            else:
                au = author
            lines.append(f"AU  - {au}")
            if author_aff and i < len(author_aff):
                for idx in (author_aff[i] or []):
                    if idx < len(aff_list):
                        lines.extend(_wrap_nbib_field("AD", aff_list[idx]))

        lines.append("LA  - eng")
        lines.append("PT  - Journal Article")

        if epub_date:
            dep = re.sub(r"[^0-9]", "", epub_date)[:8]
            if dep:
                lines.append(f"DEP - {dep}")

        lines.append(f"JT  - {row['journal']}")
        lines.append(f"TA  - {row['journal']}")

        all_kw = mesh_terms + [k for k in keywords if k not in mesh_terms]
        for kw in all_kw:
            lines.append(f"OT  - {kw}")

        if row["doi"]:
            lines.append(f"AID - {row['doi']} [doi]")

        so = row["journal"]
        if pub_date:
            so += f". {pub_date}"
        if row["pmid"]:
            so += f"; PMID: {row['pmid']}"
        if row["doi"]:
            so += f". doi: {row['doi']}"
        lines.extend(_wrap_nbib_field("SO", so))

        lines.append("")

    return "\n".join(lines)


def export_nbib(user_id: int, pmids: list[str]) -> str:
    placeholders = ",".join("?" * len(pmids))
    with conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM papers WHERE pmid IN ({placeholders})", pmids
        ).fetchall()
    return papers_to_nbib(rows)


def export_ris(user_id: int, pmids: list[str]) -> str:
    placeholders = ",".join("?" * len(pmids))
    now = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM papers WHERE pmid IN ({placeholders})", pmids
        ).fetchall()
        conn.execute(
            f"""UPDATE user_papers SET ris_exported_at = ?
                WHERE user_id = ? AND pmid IN ({placeholders})""",
            [now, user_id] + pmids,
        )
    return papers_to_ris(rows)
