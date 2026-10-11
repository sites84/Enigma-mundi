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
import email.utils
import html
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
MAX_LINKS_PER_SITE = 250
FRESHNESS_DAYS = 90
DELAY_SECONDS = 0.6
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".mp3", ".css", ".js")
SKIP_PARTS = ("/tag/", "/tags/", "/author/", "/authors/", "/category/", "/categories/", "/page/", "/search", "/login", "/signup", "/privacy", "/contact", "/about", "/feed", "/rss", "/subscribe", "/subscription", "/membership", "/donate", "/support", "/shop", "/bookstore", "/books", "/store", "/links", "/sitemap", "/newsletter", "/advertise", "/submit", "/pitch", "/careers", "/jobs", "/events", "/calendar")


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
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items = [(k, v) for k, v in query_items if k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"} and not k.lower().startswith("utm_")]
    query = urllib.parse.urlencode(sorted(query_items))
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


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
    if len(segments) == 1 and len(segments[0]) < 12 and not re.search(r"\d{4}", segments[0]):
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
        date = (parser.meta.get("article:published_time") or parser.meta.get("schema:datepublished") or
                parser.meta.get("datepublished") or parser.meta.get("date") or parser.meta.get("pubdate") or
                parser.meta.get("parsely-pub-date") or parser.meta.get("publish-date") or "")
        description = (parser.meta.get("og:description") or parser.meta.get("description") or
                       parser.meta.get("twitter:description") or "").strip()
        schema_article = bool(parser.schema_types.intersection({
            "article", "newsarticle", "blogposting", "reportagenewsarticle"
        }))
        generic_titles = {
            "home", "homepage", "subscribe", "subscriptions", "links", "contact",
            "about", "privacy policy", "terms of use", "bookstore", "sitemap",
            "pitch an article", "search results", "login", "sign in"
        }
        title_key = re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()
        # Require meaningful metadata and evidence that the page is an article.
        if title_key in generic_titles or len(title) < 18 or len(description) < 35:
            return None
        if not date and not schema_article:
            return None
        editorial_status = "discovered" if date else "needs_date_review"
        age_days = None
        if date:
            try:
                parsed_date = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if parsed_date.tzinfo is None:
                    parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - parsed_date.astimezone(timezone.utc)).days
                if age_days > 90:
                    editorial_status = "stale_archive"
            except (ValueError, TypeError):
                editorial_status = "needs_date_review"
        return {
            "source_id": site_id, "source_name": site_name, "url": canonical,
            "title": title[:300], "description": description[:1000],
            "published_at": date[:80] or None, "has_article_schema": schema_article,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status, "content_type": content_type.split(";", 1)[0],
            "status": editorial_status,
            "notes": "Metadados detectados; ainda exige verificação editorial. Não publicado.",
        }
    except Exception:
        return None



def parse_feed_entries(feed_url, source, robots_cache):
    """Read RSS/Atom entries with their original publication dates."""
    if not allowed_by_robots(feed_url, feed_url, robots_cache):
        return [], "feed disallowed by robots.txt"
    try:
        final_url, content_type, body, status = fetch(feed_url)
        root = ET.fromstring(body)
    except Exception as exc:
        return [], f"feed unavailable: {str(exc)[:160]}"
    entries = []
    for node in list(root.iter()):
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in list(node):
            key = child.tag.rsplit("}", 1)[-1].lower()
            text_value = (child.text or "").strip()
            if key == "link":
                href = child.attrib.get("href", "").strip()
                if href and (child.attrib.get("rel", "alternate") == "alternate"):
                    fields["link"] = href
                elif text_value and not fields.get("link"):
                    fields["link"] = text_value
            elif key in ("title", "description", "summary", "published", "pubdate", "date", "updated", "encoded"):
                fields.setdefault(key, text_value)
        link = normalize_url(urllib.parse.urljoin(final_url, fields.get("link", "")))
        if not link:
            continue
        host = urllib.parse.urlsplit(source.get("site_url", "")).hostname
        if host and urllib.parse.urlsplit(link).hostname != host:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", fields.get("title", "")).strip())
        desc = html.unescape(re.sub(r"<[^>]+>", " ", fields.get("description") or fields.get("summary") or fields.get("encoded") or "").strip())
        raw_date = fields.get("published") or fields.get("pubdate") or fields.get("date") or fields.get("updated") or ""
        parsed_date = None
        if raw_date:
            try:
                parsed_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed_date = email.utils.parsedate_to_datetime(raw_date)
                except (TypeError, ValueError, OverflowError):
                    parsed_date = None
        if parsed_date and parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        if not title or len(title) < 12 or not parsed_date:
            continue
        parsed_date = parsed_date.astimezone(timezone.utc)
        age_days = (datetime.now(timezone.utc) - parsed_date).days
        if age_days < 0 or age_days > FRESHNESS_DAYS:
            continue
        if len(desc) < 25:
            desc = title
        entries.append({
            "source_id": source.get("id"), "source_name": source.get("name"),
            "url": link, "title": title[:300], "description": desc[:1000],
            "published_at": parsed_date.isoformat(), "has_article_schema": True,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status, "content_type": content_type.split(";", 1)[0],
            "status": "discovered",
            "notes": "Descoberto por RSS/Atom; data original dentro da janela de 90 dias; requer revisão editorial.",
        })
    return entries, None


def main():
    config = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    sources = config.get("sources", [])
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"version": 1, "articles": [], "source_errors": []}
    # Revalidate legacy queue entries; older runs admitted generic pages and undated archives.
    articles = []
    rejected_legacy = 0
    for item in existing.get("articles", []):
        title = str(item.get("title", "")).strip()
        description = str(item.get("description", "")).strip()
        status = item.get("status")
        date = item.get("published_at")
        schema_flag = bool(item.get("has_article_schema"))
        if title and len(title) >= 18 and len(description) >= 35 and (date or schema_flag) and status in ("discovered", "needs_date_review", "stale_archive"):
            if date:
                try:
                    parsed_date = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - parsed_date.astimezone(timezone.utc)).days > 90:
                        item["status"] = "stale_archive"
                        item["notes"] = "Conteúdo com mais de 90 dias; preservado como arquivo, fora da fila de notícias recentes."
                except (ValueError, TypeError):
                    item["status"] = "needs_date_review"
            articles.append(item)
        else:
            rejected_legacy += 1
    known_urls = {normalize_url(a.get("url", "")) for a in articles if a.get("url")}
    errors = []
    source_results = []
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
        feed_entries = []
        feed_url = (source.get("feed_url") or "").strip()
        if feed_url and source.get("feed_type") == "rss":
            feed_entries, feed_error = parse_feed_entries(feed_url, source, robots_cache)
            if feed_error:
                errors.append({"source_id": source.get("id"), "error": feed_error})
            for entry in feed_entries:
                normalized = normalize_url(entry.get("url", ""))
                if normalized and normalized not in known_urls:
                    articles.append(entry)
                    known_urls.add(normalized)
        try:
            for sitemap_url in sitemap_candidates(site_url):
                candidates.extend(parse_sitemap(sitemap_url, parsed.hostname, robots_cache))
                if len(candidates) >= MAX_LINKS_PER_SITE:
                    break
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
        recent_count = sum(1 for a in articles if a.get("source_id") == source.get("id") and a.get("status") == "discovered" and a.get("published_at"))
        source_results.append({
            "source_id": source.get("id"), "source_name": source.get("name"),
            "site_url": site_url, "configured_feed": feed_url or None,
            "feed_type": source.get("feed_type"), "recent_items_from_feed": len(feed_entries),
            "recent_items_in_queue": recent_count, "candidate_urls_checked": len(candidates),
            "new_article_pages_registered": new_count,
            "result": "recent_items_found" if recent_count else ("errors" if any(e.get("source_id") == source.get("id") for e in errors) else "no_recent_items_detected")
        })
        print(f"  RSS recentes: {len(feed_entries)}; URLs candidatas: {len(candidates)}; novas páginas registradas: {new_count}", flush=True)
        time.sleep(DELAY_SECONDS)

    # Keep newest discoveries first; preserve history, with a generous cap.
    articles.sort(key=lambda a: a.get("discovered_at", ""), reverse=True)
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(sources),
        "candidate_urls_checked": total_candidates,
        "new_articles_total": len(articles),
        "pending_editorial_review": sum(1 for a in articles if a.get("status") in ("discovered", "needs_date_review")),
        "stale_archive_count": sum(1 for a in articles if a.get("status") == "stale_archive"),
        "freshness_window_days": 90,
        "legacy_records_rejected_by_quality_filter": rejected_legacy,
        "articles": articles[:10000],
        "source_errors": errors[-500:],
        "source_results": source_results,
        "notice": "Fila de descoberta apenas. Nenhum artigo foi reescrito ou publicado automaticamente.",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Total registrado na fila: {len(payload['articles'])}; erros de fonte nesta execução: {len(errors)}")


if __name__ == "__main__":
    main()
