import json
import re
from datetime import datetime, timezone

from app.db import conn_ctx


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


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
