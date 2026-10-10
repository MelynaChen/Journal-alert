"""Read-only public API smoke test; no state files, credentials or notifications."""
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jalert.conferences import Client, crossref, openalex, dblp, failure_note
from jalert.fetch import fetch_crossref, fetch_openalex, http_request, parse_feed
from jalert.state import Store


def main():
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("probe")
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))["conferences"]
    cfg.update(timeout_seconds=15, retries=0, max_pages=8)
    since = (date.today() - timedelta(days=30)).isoformat()
    with Store(":memory:") as store:
        client = Client(cfg, store)
        for name, source in [("FAST", "dblp"), ("IEDM", "crossref"), ("IRPS", "crossref"), ("VLSI Symposium", "crossref"), ("FAST", "openalex")]:
            venue = next(v for v in cfg["venues"] if v["name"] == name)
            try:
                items = {"dblp": dblp, "crossref": crossref, "openalex": openalex}[source](client, venue, since, False, log)
                print(source, name, "OK", len(items), flush=True)
                for item in items[:2]:
                    print(json.dumps(item.as_dict(), ensure_ascii=True), flush=True)
                if source == "crossref" and items:
                    work = client.get("https://api.openalex.org/works/https://doi.org/" + items[0].doi)
                    print("OpenAlex DOI enrichment OK", work.get("doi"), bool(work.get("abstract_inverted_index")), flush=True)
            except Exception as exc:
                print(source, name, "FAIL", failure_note(exc), flush=True)
        for source, fn in [("openalex", fetch_openalex), ("crossref", fetch_crossref)]:
            try:
                items = fn({"name": "IEEE Transactions on Electron Devices", "issn": "0018-9383"}, since, 1, log, timeout=15, retries=0)
                print("journal", source, "OK", len(items), flush=True)
            except Exception as exc:
                print("journal", source, "FAIL", type(exc).__name__, flush=True)
        try:
            _, raw = http_request("https://www.nature.com/natelectron.rss", timeout=15, retries=0)
            print("journal rss OK", len(parse_feed(raw, {"name": "Nature Electronics"})), flush=True)
        except Exception as exc:
            print("journal rss FAIL", type(exc).__name__, flush=True)


if __name__ == "__main__":
    main()
