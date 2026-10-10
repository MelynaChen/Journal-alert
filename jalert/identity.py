"""Conservative identities shared by conference sources and the history ledger."""
import hashlib
import re
import unicodedata
from urllib.parse import unquote


def normalize(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value or "").casefold()))


def doi_key(value):
    value = unquote(value or "").strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(". ")


def fingerprint(item):
    if getattr(item, "paper_type", "journal") != "conference" or not item.year or not item.venue:
        return ""
    # Venue/year prevents collapsing a journal extension or another edition.
    raw = f"{normalize(item.title)}|{normalize(item.venue)}|{item.year}"
    return "conference:" + hashlib.sha256(raw.encode()).hexdigest()


def same_paper(a, b):
    da, db = doi_key(a.doi), doi_key(b.doi)
    if da and db:
        return da == db
    return bool(fingerprint(a) and fingerprint(a) == fingerprint(b))


def merge_items(items):
    merged = []
    for item in items:
        item.doi = doi_key(item.doi)
        if item.doi:
            item.uid = "doi:" + item.doi
        existing = next((x for x in merged if x.uid == item.uid or same_paper(x, item)), None)
        if existing is None:
            merged.append(item)
            continue
        for attr in ("doi", "url", "authors", "date"):
            if not getattr(existing, attr):
                setattr(existing, attr, getattr(item, attr))
        if len(item.abstract or "") > len(existing.abstract or ""):
            existing.abstract = item.abstract
        if item.paper_type == "conference":
            existing.paper_type, existing.venue, existing.year = item.paper_type, item.venue, item.year
            existing.priority = item.priority
            existing.initial = existing.initial or item.initial
        if existing.doi:
            existing.uid = "doi:" + existing.doi
    return merged
