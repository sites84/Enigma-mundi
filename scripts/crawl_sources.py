#!/usr/bin/env python3
"""Discover newly published articles across configured sites without requiring RSS.

This is a discovery/queue stage only: it does not rewrite or publish content.
It uses sitemaps and ordinary HTML links, respects robots.txt disallow rules
for the crawler user-agent, applies per-site rate limits, and records URLs
idempotently in data/discovered-articles.json.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "config" / "sources.json"
OUTPUT_PATH = ROOT / "data" / "discovered-articles.json"
USER_AGENT = "EnigmaMundiBot/1.0 (+https://github.com/sites84/Enigma-mundi)"
TIMEOUT = 12
MAX_BYTES = 2_000_000
MAX_SITEMAP_URLS = 400
MAX_LINKS_PER_SITE = 80
DELAY_SECONDS = 0.6
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".mp3", ".css", ".js")
SKIP_PARTS = ("/tag/", "/tags/", "/author/", "/authors/", "/category/", "/categories/", "/page/", "/search", "/login", "/signup", "/privacy", "/contact", "/about", "/feed", "/rss")


def fetch(url: str):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xml,text/xml,application/xhtml+xml;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read(MAX_BYTES)
        return response.geturl(), response.headers.get("Content-Type", ""), body, response.status


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.title = []
        self.in_title = False
        self.meta = {}
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            value = attrs.get("content", "").strip()
            if key and value:
                self.meta[key] = value
        elif tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonical = attrs.get("href")

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and data.strip():
            self.title.append(data.strip())


def normalize_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def likely_article(url, base_host):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname != base_host:
        return False
    path = parsed.path.lower()
    if not path or path == "/" or any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False
    if any(part in path for part in SKIP_PARTS):
        return False
    segments = [x for x in path.split("/") if x]
    if not segments:
        return False
    # Avoid common utility and pagination paths; accept typical article slugs.
    if len(segments) == 1 and len(segments[0]) < 12 and not re.search(r"\\d{4}", segments[0]):
        return False
    return True


def allowed_by_robots(site_url, target_url, cache):
    parsed = urllib.parse.urlsplit(site_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
        try:
            rp.read()
            cache[origin] = rp
        except Exception:
            # If robots.txt cannot be read, fail closed for this origin.
            cache[origin] = False
    rules = cache[origin]
    if rules is False:
        return False
    return rules.can_fetch(USER_AGENT, target_url)


def sitemap_candidates(site_url):
    parsed = urllib.parse.urlsplit(site_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found = []
    robots = urllib.parse.urljoin(origin, "/robots.txt")
    try:
        _, _, body, _ = fetch(robots)
        for line in body.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("sitemap:"):
                found.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    found.extend([urllib.parse.urljoin(origin, "/sitemap.xml"), urllib.parse.urljoin(origin, "/sitemap_index.xml")])
    return list(dict.fromkeys(found))


def parse_sitemap(url, base_host, robots_cache, seen_sitemaps=None, depth=0):
    if seen_sitemaps is None:
        seen_sitemaps = set()
    if url in seen_sitemaps or depth > 2 or len(seen_sitemaps) > 12:
        return []
    seen_sitemaps.add(url)
    if not allowed_by_robots(url, url, robots_cache):
        return []
    try:
        final_url, _, body, _ = fetch(url)
        root = ET.fromstring(body)
    except Exception:
        return []
    tag = root.tag.rsplit("}", 1)[-1].lower()
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1].lower() == "loc" and el.text]
    results = []
    if tag == "sitemapindex":
        for child in locs[:12]:
            results.extend(parse_sitemap(child, base_host, robots_cache, seen_sitemaps, depth + 1))
            if len(results) >= MAX_SITEMAP_URLS:
                break
    else:
        for loc in locs:
            normalized = normalize_url(loc)
            if normalized and likely_article(normalized, base_host) and allowed_by_robots(final_url, normalized, robots_cache):
                results.append(normalized)
            if len(results) >= MAX_SITEMAP_URLS:
                break
    return results[:MAX_SITEMAP_URLS]


def article_metadata(url, site_id, site_name, robots_cache):
    if not allowed_by_robots(url, url, robots_cache):
        return None
    try:
        final_url, content_type, body, status = fetch(url)
        if "html" not in content_type.lower() and body.lstrip()[:20].lower().startswith(b"<?xml"):
            return None
        parser = PageParser()
        parser.feed(body.decode("utf-8", "replace"))
        title = (parser.meta.get("og:title") or parser.meta.get("twitter:title") or " ".join(parser.title)).strip()
        canonical = normalize_url(urllib.parse.urljoin(final_url, parser.canonical)) if parser.canonical else normalize_url(final_url)
        if not title or not canonical:
            return None
        date = (parser.meta.get("article:published_time") or parser.meta.get("datepublished") or
                parser.meta.get("date") or parser.meta.get("pubdate") or "")
        description = (parser.meta.get("og:description") or parser.meta.get("description") or "").strip()
        return {
            "source_id": site_id, "source_name": site_name, "url": canonical,
            "title": title[:300], "description": description[:1000],
            "published_at": date[:80] or None, "discovered_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status, "content_type": content_type.split(";", 1)[0],
            "status": "discovered", "notes": "Aguardando verificação editorial; não publicado.",
        }
    except Exception:
        return None


def main():
    config = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    sources = config.get("sources", [])
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"version": 1, "articles": [], "source_errors": []}
    articles = existing.get("articles", [])
    known_urls = {normalize_url(a.get("url", "")) for a in articles}
    errors = []
    robots_cache = {}
    total_candidates = 0

    for index, source in enumerate(sources, 1):
        site_url = (source.get("site_url") or "").strip()
        if source.get("enabled") is False or not site_url:
            # Disabled sources are still eligible for discovery; enabled controls
            # editorial publishing only. Do not skip source discovery here.
            pass
        parsed = urllib.parse.urlsplit(site_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            errors.append({"source_id": source.get("id"), "error": "URL de site inválida"})
            continue
        print(f"[{index}/{len(sources)}] {source.get('name', source.get('id'))}", flush=True)
        candidates = []
        try:
            candidates.extend(parse_sitemap(site_url, parsed.hostname, robots_cache))
        except Exception as exc:
            errors.append({"source_id": source.get("id"), "error": str(exc)[:200]})
        # Also scan the homepage for article links so sites without sitemaps work.
        try:
            if allowed_by_robots(site_url, site_url, robots_cache):
                final_url, _, body, _ = fetch(site_url)
                parser = PageParser()
                parser.feed(body.decode("utf-8", "replace"))
                for href in parser.links:
                    absolute = normalize_url(urllib.parse.urljoin(final_url, href))
                    if absolute and likely_article(absolute, parsed.hostname):
                        candidates.append(absolute)
                    if len(candidates) >= MAX_LINKS_PER_SITE:
                        break
        except Exception as exc:
            errors.append({"source_id": source.get("id"), "error": str(exc)[:200]})
        candidates = list(dict.fromkeys(candidates))[:MAX_LINKS_PER_SITE]
        total_candidates += len(candidates)
        new_count = 0
        for url in candidates:
            if url in known_urls:
                continue
            metadata = article_metadata(url, source.get("id"), source.get("name"), robots_cache)
            if metadata:
                articles.append(metadata)
                known_urls.add(metadata["url"])
                new_count += 1
            time.sleep(DELAY_SECONDS)
        print(f"  URLs candidatas: {len(candidates)}; novas registradas: {new_count}", flush=True)
        time.sleep(DELAY_SECONDS)

    # Keep newest discoveries first; preserve history, with a generous cap.
    articles.sort(key=lambda a: a.get("discovered_at", ""), reverse=True)
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(sources),
        "candidate_urls_checked": total_candidates,
        "new_articles_total": len(articles),
        "pending_editorial_review": sum(1 for a in articles if a.get("status") == "discovered"),
        "articles": articles[:10000],
        "source_errors": errors[-500:],
        "notice": "Fila de descoberta apenas. Nenhum artigo foi reescrito ou publicado automaticamente.",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Total registrado na fila: {len(payload['articles'])}; erros de fonte nesta execução: {len(errors)}")


if __name__ == "__main__":
    main()
