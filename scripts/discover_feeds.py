#!/usr/bin/env python3
"""Discover and validate RSS/Atom endpoints for configured editorial sources.

This job does not ingest or publish articles. It only checks the source registry
and writes a machine-readable health report for review.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCES_FILE = ROOT / "config" / "sources.json"
OUTPUT_FILE = ROOT / "data" / "source-status.json"
TIMEOUT_SECONDS = 12
USER_AGENT = "EnigmaMundiFeedDiscovery/1.0 (+https://github.com/sites84/Enigma-mundi)"
FEED_TYPES = {
    "application/rss+xml", "application/atom+xml", "application/rdf+xml",
    "application/xml", "text/xml",
}
FALLBACK_PATHS = ("/feed", "/feed/", "/feed.xml", "/rss", "/rss.xml", "/atom.xml")


class AlternateFeedParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        values = {k.lower(): (v or "") for k, v in attrs}
        rel = {part.lower() for part in values.get("rel", "").split()}
        media_type = values.get("type", "").split(";", 1)[0].strip().lower()
        if "alternate" in rel and (media_type in FEED_TYPES or "rss" in media_type or "atom" in media_type):
            href = values.get("href", "").strip()
            if href:
                self.urls.append(href)


def request_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL não é HTTP/HTTPS válida")
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.5"})
    with urlopen(req, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        final_url = response.geturl()
        body = response.read(1_500_000)
        return final_url, content_type, body, response.status


def validate_feed(url: str):
    try:
        final_url, content_type, body, status = request_url(url)
        root = ET.fromstring(body)
        tag = root.tag.lower() if isinstance(root.tag, str) else ""
        local_tag = tag.rsplit("}", 1)[-1].split(":")[-1]
        if local_tag in ("rss", "rdf"):
            kind = "rss"
        elif local_tag == "feed":
            kind = "atom"
        else:
            return None
        return {
            "url": final_url,
            "type": kind,
            "http_status": status,
            "content_type": content_type,
            "title": extract_title(root),
        }
    except (HTTPError, URLError, TimeoutError, ValueError, ET.ParseError, OSError) as exc:
        return None


def extract_title(root):
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1].lower() if isinstance(elem.tag, str) else ""
        if local == "title" and elem.text and elem.text.strip():
            return elem.text.strip()[:180]
    return None


def discover(source):
    base = source.get("site_url") or source.get("feed_url") or ""
    result = {
        "id": source.get("id"),
        "name": source.get("name"),
        "category": source.get("category"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "not_found",
        "feed_url": None,
        "feed_type": None,
        "feed_title": None,
        "http_status": None,
        "error": None,
    }
    try:
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL base inválida")
        candidates = [base]
        final_url, content_type, body, status = request_url(base)
        result["http_status"] = status
        if "xml" in content_type or "rss" in content_type or "atom" in content_type:
            feed = validate_feed(final_url)
            if feed:
                result.update(status="valid", feed_url=feed["url"], feed_type=feed["type"], feed_title=feed["title"])
                return result
        parser = AlternateFeedParser()
        try:
            parser.feed(body.decode("utf-8", errors="replace"))
        except Exception:
            pass
        candidates.extend(urljoin(final_url, item) for item in parser.urls)
        root = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        candidates.extend(urljoin(root, path) for path in FALLBACK_PATHS)
        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            feed = validate_feed(candidate)
            if feed:
                result.update(status="valid", feed_url=feed["url"], feed_type=feed["type"], feed_title=feed["title"], http_status=feed["http_status"])
                return result
        result["status"] = "no_feed_found"
    except HTTPError as exc:
        result["status"] = "http_error"
        result["http_status"] = exc.code
        result["error"] = str(exc)[:240]
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        result["status"] = "unreachable"
        result["error"] = str(exc)[:240]
    return result


def main():
    try:
        config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        sources = config.get("sources", [])
    except Exception as exc:
        print(f"Não foi possível ler config/sources.json: {exc}", file=sys.stderr)
        return 1

    results = []
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        print(f"[{index}/{total}] Verificando {source.get('name', source.get('id'))}", flush=True)
        results.append(discover(source))
        time.sleep(0.2)

    counts = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(results),
        "summary": counts,
        "sources": results,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
