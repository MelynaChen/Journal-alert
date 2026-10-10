"""Public metadata adapters; no fabricated publication dates or title-only venue guesses."""
from datetime import date, timedelta
import json
import re
import time
from urllib.parse import urlencode

from .fetch import FetchError, Item, http_request, _clean, _doi_from, _inverted_abstract, _parse_date
from .identity import doi_key, fingerprint, normalize, merge_items


def venue_matches(value, venue):
    value = normalize(value)
    # Remove edition/year/publisher scaffolding, then require a complete alias.
    value = re.sub(r"\b(?:19|20)\d{2}\b|\b\d+(?:st|nd|rd|th)?\b", " ", value)
    value = re.sub(r"\b(?:proceedings|of|the|ieee|acm|annual)\b", " ", value)
    value = " ".join(value.split())
    aliases = venue.get("aliases", []) + [venue["name"]]
    for alias in aliases:
        alias = normalize(alias)
        alias = re.sub(r"\b(?:of|the|ieee|acm|annual)\b", " ", alias)
        alias = " ".join(alias.split())
        if value == alias:
            return True
        # Full names may have their acronym appended in parentheses.
        if len(alias) > 15 and any(value == alias + " " + normalize(short) for short in aliases):
            return True
    return False


def failure_note(exc):
    if isinstance(exc, FetchError):
        return str(exc).rsplit(" -> ", 1)[-1][:200]
    if isinstance(exc, ValueError):
        return str(exc)[:200]
    return f"{type(exc).__name__}: source query failed or incomplete"


class Client:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.last = 0.0
        self.deadline = time.monotonic() + int(cfg.get("max_run_seconds", 600))

    def get(self, url):
        if time.monotonic() >= self.deadline:
            raise ValueError("conference run time budget reached; remaining sources not scanned")
        cached = self.store.cached(url, int(self.cfg.get("cache_hours", 12)))
        if cached is not None:
            return cached
        time.sleep(max(0, float(self.cfg.get("request_interval_seconds", 1)) - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        _, raw = http_request(url, timeout=int(self.cfg.get("timeout_seconds", 20)),
                              retries=int(self.cfg.get("retries", 1)))
        payload = json.loads(raw)
        self.store.cache(url, payload)
        return payload


def make_item(venue, source, title, year=0, doi="", url="", published="", authors=None, abstract=""):
    item = Item(uid="", title=_clean(title), doi=doi_key(doi), url=url,
                journal=venue["name"], venue=venue["name"], year=int(year or 0),
                date=published, authors=authors or [], abstract=_clean(abstract),
                source=source, paper_type="conference", priority=int(venue.get("priority", 0)))
    item.uid = "doi:" + item.doi if item.doi else fingerprint(item)
    if not item.uid:
        raise ValueError("conference record lacks DOI or venue/year identity")
    return item


def edition_year(names, venue, fallback=0):
    years = {int(y) for name in names if venue_matches(name, venue)
             for y in re.findall(r"\b(?:19|20)\d{2}\b", name)}
    return next(iter(years)) if len(years) == 1 else fallback


def dblp(client, venue, since, initial, log):
    streams = venue.get("dblp_streams", [])
    if not streams:
        raise ValueError("no DBLP stream configured; use Crossref/OpenAlex")
    items = []
    size = int(client.cfg.get("page_size", 100))
    pages = int(client.cfg.get("max_pages", 8))
    # DBLP has no dependable publication day. Rescan past editions for late indexing.
    floor = date.today().year - int(client.cfg.get("rescan_years", 3))
    for stream, requested_year in [(s, y) for s in streams for y in range(floor, date.today().year + 1)]:
        query = f"stream:{stream}: year:{requested_year}:"
        for page in range(pages):
            payload = client.get("https://dblp.org/search/publ/api?" + urlencode(
                {"q": query, "format": "json", "h": size, "f": page * size}))
            hits = payload["result"]["hits"]
            records = hits.get("hit", [])
            if isinstance(records, dict):
                records = [records]
            for hit in records:
                info = hit.get("info", {})
                key = info.get("key", "")
                if not key.startswith(stream + "/") or info.get("type") != "Conference and Workshop Papers":
                    continue
                # Exact bibliographic venue additionally rejects workshops in the same stream.
                names = info.get("venue", [])
                if isinstance(names, str):
                    names = [names]
                if not any(venue_matches(n, venue) for n in names):
                    log.debug("filtered DBLP venue: %s", names)
                    continue
                year = int(info.get("year", 0))
                if year != requested_year:
                    continue
                authors = (info.get("authors") or {}).get("author", [])
                if isinstance(authors, (dict, str)):
                    authors = [authors]
                ee = info.get("ee") or ""
                if isinstance(ee, list):
                    ee = ee[0] if ee else ""
                items.append(make_item(venue, "dblp", info.get("title", ""), year,
                    _doi_from(info.get("doi", ""), ee), ee or info.get("url", ""),
                    authors=[a.get("text", "") if isinstance(a, dict) else a for a in authors]))
            if (page + 1) * size >= int(hits.get("@total", 0)):
                break
        else:
            raise ValueError(f"DBLP pagination cap ({pages} pages); increase max_pages; scan incomplete")
    return items


def crossref(client, venue, since, initial, log):
    # Crossref query.container-title is relevance search, NOT a venue filter.
    # Discover exact containers first, then paginate each exact container.
    containers = list(venue.get("crossref_container_titles", []))
    if not containers:
        discovery = client.get("https://api.crossref.org/works?" + urlencode({
            "query.container-title": venue.get("query") or venue.get("aliases", [venue["name"]])[0],
            "filter": f"type:proceedings-article,from-pub-date:{date.today().year - int(client.cfg.get('rescan_years', 3))}-01-01",
            "rows": 100, "select": "container-title"}))
        containers = sorted({n for w in discovery["message"].get("items", [])
                             for n in w.get("container-title", []) if venue_matches(n, venue)})
    if not containers:
        raise ValueError("Crossref: no verified container title; coverage unavailable")
    items = []
    for container in containers:
        if not venue_matches(container, venue):
            raise ValueError("Crossref configured container does not match venue aliases")
        items.extend(_crossref_container(client, venue, since, container, log))
    return items


def _crossref_container(client, venue, since, container, log):
    cursor = "*"
    items = []
    size = int(client.cfg.get("page_size", 100))
    for _ in range(int(client.cfg.get("max_pages", 8))):
        payload = client.get("https://api.crossref.org/works?" + urlencode({
            "filter": f"type:proceedings-article,container-title:{container},from-index-date:{since}",
            "rows": size, "cursor": cursor,
            "select": "DOI,title,container-title,event,published,issued,author,abstract,URL"}))
        message = payload["message"]
        records = message.get("items", [])
        for work in records:
            names = work.get("container-title", []) + [(work.get("event") or {}).get("name", "")]
            if not any(venue_matches(n, venue) for n in names):
                log.debug("filtered Crossref venue: %s", names)
                continue
            parts = ((work.get("published") or work.get("issued") or {}).get("date-parts") or [[]])[0]
            published = ""
            if len(parts) == 3:
                try:
                    published = date(*parts).isoformat()
                except (ValueError, TypeError):
                    pass
            title = (work.get("title") or [""])[0]
            items.append(make_item(venue, "crossref", title, edition_year(names, venue, parts[0] if parts else 0),
                work.get("DOI", ""), work.get("URL", ""), published,
                [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in (work.get("author") or [])],
                work.get("abstract", "")))
        if len(records) < size:
            return items
        next_cursor = message.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            raise ValueError("Crossref cursor stalled; incomplete scan")
        cursor = next_cursor
    raise ValueError("Crossref pagination cap; increase max_pages; scan incomplete")


def openalex(client, venue, since, initial, log):
    ids = venue.get("openalex_source_ids", [])
    if not ids:
        sources = client.get("https://api.openalex.org/sources?" + urlencode(
            {"search": venue.get("query") or venue.get("aliases", [venue["name"]])[0], "per-page": 50})).get("results", [])
        ids = [s["id"] for s in sources if s.get("type") == "conference" and venue_matches(s.get("display_name", ""), venue)]
    if not ids:
        raise ValueError("OpenAlex: no verified conference source; DOI enrichment remains available")
    items = []
    for source_id in ids:
        cursor = "*"
        for _ in range(int(client.cfg.get("max_pages", 8))):
            payload = client.get("https://api.openalex.org/works?" + urlencode({
                "filter": f"locations.source.id:{source_id},from_publication_date:{date.today().year - int(client.cfg.get('rescan_years', 3))}-01-01",
                "per-page": int(client.cfg.get("page_size", 100)), "cursor": cursor,
                "select": "id,doi,title,publication_year,publication_date,primary_location,locations,authorships,abstract_inverted_index"}))
            for work in payload.get("results", []):
                sources = [loc.get("source") or {} for loc in (work.get("locations") or [])]
                if not any(s.get("id") == source_id and (source_id in venue.get("openalex_source_ids", []) or venue_matches(s.get("display_name", ""), venue)) for s in sources):
                    continue
                loc = work.get("primary_location") or {}
                items.append(make_item(venue, "openalex", work.get("title", ""), edition_year([s.get("display_name", "") for s in sources], venue, work.get("publication_year")),
                    work.get("doi", ""), loc.get("landing_page_url") or work.get("doi") or "",
                    _parse_date(work.get("publication_date")),
                    [(a.get("author") or {}).get("display_name", "") for a in (work.get("authorships") or [])],
                    _inverted_abstract(work.get("abstract_inverted_index"))))
            next_cursor = (payload.get("meta") or {}).get("next_cursor")
            if not next_cursor or not payload.get("results"):
                break
            if next_cursor == cursor:
                raise ValueError("OpenAlex cursor stalled")
            cursor = next_cursor
        else:
            raise ValueError("OpenAlex pagination cap; increase max_pages")
    return items


def collect_conferences(cfg, store, log):
    settings = cfg.get("conferences", {})
    if not settings.get("enabled", False):
        return [], []
    client = Client(settings, store)
    items, statuses = [], []
    for venue in settings.get("venues", []):
        if not venue.get("enabled", True):
            continue
        for source in settings.get("sources", ["dblp", "openalex", "crossref"]):
            initial = not store.scanned(venue["name"], source)
            days = int(settings.get("initial_lookback_days", 365) if initial else settings.get("lookback_days", 30))
            since = (date.today() - timedelta(days=days)).isoformat()
            status = {"journal": venue["name"], "source": source, "ok": False, "items": 0, "note": "", "initial": initial}
            try:
                adapter = {"dblp": dblp, "crossref": crossref, "openalex": openalex}[source]
                records = adapter(client, venue, since, initial, log)
                for item in records:
                    item.initial = initial
                    if not item.title:
                        continue
                    # Year-only records use an explicitly coarse initial year boundary.
                    if initial and ((item.date and item.date < since) or (not item.date and item.year and item.year < int(since[:4]))):
                        item.initial_hidden = True
                    items.append(item)
                status.update(ok=True, items=len(records), note="" if records else "no verified records; coverage may be incomplete")
                log.info("conference %s %s: %d records%s", venue["name"], source, len(records), " (initial)" if initial else "")
                if not records:
                    log.warning("conference %s %s: no verified records; coverage unknown", venue["name"], source)
            except Exception as exc:
                # Do not print request URLs: future API credentials may occur there.
                status["note"] = failure_note(exc)
                log.warning("conference %s %s FAILED: %s", venue["name"], source, status["note"])
            statuses.append(status)
    items = merge_items(items)
    # DOI is authoritative. No fuzzy title enrichment, so namesakes cannot merge.
    budget = int(settings.get("enrichment_limit", 30))
    if "openalex" in settings.get("sources", []):
        from .score import Scorer
        scorer = Scorer(cfg.get("keywords", []), [], [], settings.get("contextual_keywords", []))
        candidates = sorted((i for i in items if i.doi and not i.abstract), key=lambda i: scorer.score(i).score, reverse=True)
        for item in candidates[:budget]:
            try:
                work = client.get("https://api.openalex.org/works/https://doi.org/" + item.doi)
                if doi_key(work.get("doi")) != item.doi:
                    continue
                item.abstract = _clean(_inverted_abstract(work.get("abstract_inverted_index")))
                item.date = item.date or _parse_date(work.get("publication_date"))
                item.authors = item.authors or [(a.get("author") or {}).get("display_name", "") for a in (work.get("authorships") or [])]
            except Exception as exc:
                log.warning("OpenAlex DOI enrichment failed: %s", failure_note(exc))
    initial_since = (date.today() - timedelta(days=int(settings.get("initial_lookback_days", 365)))).isoformat()
    for item in items:
        if item.initial and item.date:
            item.initial_hidden = item.date < initial_since
    return items, statuses
