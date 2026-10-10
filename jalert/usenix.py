"""USENIX public technical-session pages, parsed structurally with HTMLParser.

Used when bibliographic indexes have no accessible metadata. Only node-paper
articles with a presentation link in the configured conference edition qualify.
Session dates are never repurposed as publication dates.
"""
from datetime import date
from html.parser import HTMLParser
import re
import time
from urllib.parse import urljoin, urlsplit

from .fetch import http_request, FetchError, _clean


class ScheduleParser(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.prefix = urlsplit(page_url).path.rsplit("/", 1)[0] + "/presentation/"
        self.stack = []
        self.record = None
        self.records = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "article" and "node-paper" in classes and self.record is None:
            self.record = {"title": [], "authors": [], "abstract": [], "url": ""}
            self.stack = []
        if self.record is None:
            return
        if tag == "a" and any(t == "h2" for t, _ in self.stack):
            link = urljoin(self.page_url, attrs.get("href", ""))
            parts = urlsplit(link)
            if parts.scheme == "https" and parts.netloc == "www.usenix.org" and parts.path.startswith(self.prefix) and not parts.query:
                self.record["url"] = link
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, classes))

    def handle_data(self, data):
        if self.record is None:
            return
        tags = [t for t, _ in self.stack]
        classes = {c for _, cs in self.stack for c in cs}
        if "script" in tags or "style" in tags:
            return
        if "h2" in tags and "a" in tags:
            self.record["title"].append(data)
        elif "field-name-field-paper-description-long" in classes:
            self.record["abstract"].append(data)
        elif "field-name-field-paper-people-text" in classes and "em" not in tags:
            self.record["authors"].append(data)

    def handle_endtag(self, tag):
        if self.record is None:
            return
        indices = [i for i, (t, _) in enumerate(self.stack) if t == tag]
        if not indices:
            return
        index = indices[-1]
        if tag == "article" and index == 0:
            record = self.record
            if record["url"] and record["title"] and (record["abstract"] or record["authors"]):
                self.records.append({
                    "title": _clean("".join(record["title"])), "url": record["url"],
                    "abstract": _clean(" ".join(record["abstract"])),
                    "authors": [a.strip() for a in re.split(r",|;|\band\b", "".join(record["authors"])) if a.strip()],
                })
            self.record = None
        self.stack = self.stack[:index]


def usenix(client, venue, since, initial, log):
    from .conferences import make_item, failure_note
    slug = venue["usenix_slug"]
    if not re.fullmatch(r"[a-z]+", slug):
        raise ValueError("invalid USENIX conference slug")
    last = min(date.today().year, int(venue.get("usenix_last_year", date.today().year)))
    first = date.today().year - int(client.cfg.get("rescan_years", 3))
    items = []
    for year in range(last, first - 1, -1):
        url = f"https://www.usenix.org/conference/{slug}{year % 100:02d}/technical-sessions"
        try:
            records = client.store.cached(url, int(client.cfg.get("cache_hours", 12)))
            if records is None:
                if time.monotonic() >= client.deadline:
                    raise ValueError("conference run time budget reached")
                time.sleep(max(0, float(client.cfg.get("request_interval_seconds", 1)) - (time.monotonic() - client.last)))
                client.last = time.monotonic()
                _, raw = http_request(url, timeout=int(client.cfg.get("timeout_seconds", 20)), retries=int(client.cfg.get("retries", 1)))
                parser = ScheduleParser(url)
                parser.feed(raw.decode("utf-8"))
                records = parser.records
                if not records:
                    raise ValueError("no verified node-paper records; page unavailable or format changed")
                client.store.cache(url, records)
            for record in records:
                items.append(make_item(venue, "usenix", record["title"], year,
                    url=record["url"], authors=record["authors"], abstract=record["abstract"]))
        except (FetchError, ValueError) as exc:
            note = f"USENIX {slug}{year % 100:02d}: {failure_note(exc)}"
            client.warnings.append(note)
            log.warning("%s", note)
    if not items:
        raise FetchError("USENIX: all configured editions unavailable; " + "; ".join(client.warnings))
    return items
