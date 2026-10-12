#!/usr/bin/env python3
"""Build the site's dated 90-day source-curation list from the discovery queue."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
queue = json.loads((ROOT / "data/discovered-articles.json").read_text(encoding="utf-8"))
config = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
sources = {s.get("id"): s for s in config.get("sources", [])}
now = datetime.now(timezone.utc)
cutoff = now - timedelta(days=90)
posts = []
seen = set()

for article in queue.get("articles", []):
    url = article.get("url")
    raw_date = article.get("published_at")
    if not url or url in seen or not raw_date or article.get("status") != "discovered":
        continue
    try:
        published = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)
    except (ValueError, TypeError):
        continue
    if published < cutoff or published > now:
        continue
    source = sources.get(article.get("source_id"), {})
    category = source.get("category", "misterios")
    title = str(article.get("title", "")).strip()
    excerpt = str(article.get("description", "")).strip()
    if len(title) < 12 or not excerpt:
        continue
    # Reject templates, utility pages and common non-editorial sections that
    # frequently leak through broad sitemap discovery.
    parsed_url = urlsplit(url)
    path = parsed_url.path.lower().rstrip("/")
    # A feed endpoint, domain homepage, or utility URL is never a news article.
    # These leaked into the queue when a publisher's feed exposed generic metadata.
    if path in ("", "/") or path.endswith(("/rss.xml", "/feed.xml", "/atom.xml", "/sitemap.xml", "/index.xml")):
        continue
    bad_path_parts = (
        "/tdb_templates/", "/wp-content/", "/wp-admin/", "/wp-json/",
        "/tag/", "/tags/", "/category/", "/categories/", "/author/",
        "/authors/", "/page/", "/search", "/privacy", "/cookie",
        "/terms", "/contact", "/about", "/subscribe", "/login",
        "/register", "/account", "/feed", "/rss", "/sitemap",
        "/service/", "/services/", "/servicos/", "/arquivo-central", "/acesso-ao-arquivo",
    )
    title_key = "".join(ch.lower() if ch.isalnum() else " " for ch in title).split()
    title_text = " ".join(title_key)
    bad_title_phrases = (
        "footer template", "header template", "single post template",
        "404 not found", "page not found", "privacy policy", "cookie policy",
        "terms of use", "search results", "log in", "sign in",
        "obter acesso ao arquivo central", "access the archive", "service unavailable",
    )
    if any(part in path + "/" for part in bad_path_parts) or any(phrase in title_text for phrase in bad_title_phrases):
        continue
    seen.add(url)
    posts.append({
        "id": f'{article.get("source_id", "source")}-{published.date().isoformat()}-{len(posts)+1}',
        "title": title,
        "excerpt": excerpt[:600],
        "category": category,
        "category_label": category.replace("_", " ").capitalize(),
        "published_at": published.isoformat(),
        "original_title": title,
        "source_name": article.get("source_name", source.get("name", "Fonte externa")),
        "source_url": url,
        "url": url,
        "status": "published",
        "post_type": "curated_source",
        "editorial_note": "Curadoria de publicação externa; não é reportagem original do Enigma Mundi. Verifique as alegações na fonte.",
        "image_url": article.get("image_url") or None
    })


# Preserve previously resolved images and credits across daily rebuilds.
previous_path = ROOT / "data/posts.json"
try:
    previous_payload = json.loads(previous_path.read_text(encoding="utf-8"))
    previous_by_url = {p.get("url"): p for p in previous_payload.get("posts", []) if p.get("url")}
except (OSError, json.JSONDecodeError):
    previous_by_url = {}

for post in posts:
    previous = previous_by_url.get(post["url"], {})
    for field in ("image_url", "image_alt", "image_credit", "image_license", "image_source_url"):
        if not post.get(field) and previous.get(field):
            post[field] = previous[field]

# Fill remaining image gaps with Wikimedia Commons, which provides reusable
# media plus attribution/license metadata. A filename/title relevance threshold
# prevents arbitrary unrelated pictures from being attached to stories.
import html
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has", "was",
    "were", "are", "into", "about", "after", "before", "over", "under", "new",
    "says", "said", "why", "how", "what", "when", "where", "who", "its", "their",
    "his", "her", "our", "your", "not", "but", "will", "could", "would", "should",
    "uma", "umas", "uns", "as", "os", "que", "para", "por", "com", "sem", "sobre",
    "entre", "como", "quando", "onde", "porque", "mais", "menos", "novo", "nova",
    "diz", "disse", "segundo", "pode", "podem", "ser", "foi", "são", "dos", "das",
    "the", "into", "its", "their", "his", "her", "and", "or", "of", "in", "on",
}
def image_tokens(value):
    words = re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
    return {w for w in words if w not in STOP_WORDS and not w.isdigit()}

def openverse_lookup(post):
    """Find a topic-matched image indexed from many public web sources."""
    title = post.get("title", "").strip()
    tokens = image_tokens(title)
    if not tokens:
        return None
    query_words = [word for word in re.findall(r"[a-z0-9]{3,}", title.lower()) if word not in STOP_WORDS]
    query = " ".join(query_words[:10])[:180].strip()
    if not query:
        return None
    params = {
        "q": query, "page_size": "20",
        "license_type": "commercial,modification",
        "aspect_ratio": "wide", "size": "medium",
    }
    api_url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(api_url, headers={
        "User-Agent": "EnigmaMundiEditorialBot/1.0 (editorial image matching; https://github.com/sites84/Enigma-mundi)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    best = None
    best_score = 0
    for item in (data or {}).get("results") or []:
        image_url = item.get("thumbnail") or item.get("url")
        if not image_url or not image_url.startswith("https://"):
            continue
        candidate_text = " ".join(str(item.get(k) or "") for k in ("title", "tags", "creator", "source"))
        overlap = tokens & image_tokens(candidate_text)
        distinctive = [word for word in overlap if len(word) >= 6]
        if len(overlap) < 2 and not distinctive:
            continue
        score = len(overlap) + sum(0.6 for word in distinctive)
        if score > best_score:
            best_score = score
            best = {
                "image_url": image_url,
                "image_alt": (item.get("title") or title)[:250],
                "image_credit": str(item.get("creator") or "")[:300],
                "image_license": str(item.get("license") or "")[:100],
                "image_source_url": item.get("foreign_landing_url") or item.get("detail_url") or "",
            }
    return best

def commons_lookup(post):
    title = post.get("title", "")
    tokens = image_tokens(title)
    if not tokens:
        return None
    # Search using the meaningful words, not the entire headline punctuation.
    query_words = sorted(tokens, key=lambda word: (-len(word), word))[:7]
    query = " ".join(query_words)
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": "6", "gsrsearch": query, "gsrlimit": "8",
        "prop": "imageinfo", "iiprop": "url|extmetadata",
        "iiurlwidth": "1200",
        "iiextmetadatafilter": "LicenseShortName|UsageTerms|Artist|Credit|ImageDescription",
    }
    api_url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(api_url, headers={
        "User-Agent": "EnigmaMundiEditorialBot/1.0 (public editorial project; image attribution enabled)"
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    best = None
    best_score = 0
    for page in pages.values():
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        meta = info.get("extmetadata") or {}
        license_name = html.unescape((meta.get("LicenseShortName") or {}).get("value", "")).strip()
        usage_terms = html.unescape((meta.get("UsageTerms") or {}).get("value", "")).strip()
        license_text = (license_name + " " + usage_terms).lower()
        # Only use public-domain or Creative Commons files with a declared license.
        if not license_name or not any(x in license_text for x in ("cc0", "public domain", "cc by", "cc-by", "cc by-sa", "cc-by-sa")):
            continue
        file_title = page.get("title", "").replace("File:", "").replace("_", " ")
        description = html.unescape((meta.get("ImageDescription") or {}).get("value", ""))
        artist = html.unescape((meta.get("Artist") or {}).get("value", ""))
        credit = html.unescape((meta.get("Credit") or {}).get("value", ""))
        candidate_tokens = image_tokens(file_title + " " + description)
        overlap = tokens & candidate_tokens
        # Require at least two meaningful matches, or one long distinctive term.
        score = len(overlap) + sum(0.75 for word in overlap if len(word) >= 8)
        if len(overlap) < 2 and not any(len(word) >= 9 for word in overlap):
            continue
        image_url = info.get("thumburl") or info.get("url")
        if not image_url or not image_url.startswith("https://"):
            continue
        if score > best_score:
            best_score = score
            best = {
                "image_url": image_url,
                "image_alt": title[:250],
                "image_credit": re.sub(r"<[^>]+>", "", artist or credit)[:300],
                "image_license": license_name[:100],
                "image_source_url": info.get("descriptionurl") or "",
            }
    return best

def publisher_image_lookup(post):
    """Extract the image explicitly attached to the original article page."""
    url = post.get("source_url") or post.get("url") or ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; EnigmaMundiBot/1.0; +https://github.com/sites84/Enigma-mundi)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if "html" not in response.headers.get("Content-Type", "").lower():
                return None
            page = response.read(900_000).decode("utf-8", errors="replace")
    except Exception:
        return None
    # Prefer the publisher's social preview image, then JSON-LD image metadata.
    patterns = [
        r"""<meta[^>]+(?:property|name)=["']og:image(?::secure_url)?["'][^>]+content=["']([^"']+)["']""",
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["']og:image(?::secure_url)?["']""",
        r"""<meta[^>]+(?:property|name)=["']twitter:image(?::src)?["'][^>]+content=["']([^"']+)["']""",
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["']twitter:image(?::src)?["']""",
    ]
    candidate = None
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            candidate = html.unescape(match.group(1).strip())
            break
    if not candidate:
        # JSON-LD is a common fallback for publishers that omit Open Graph.
        match = re.search(r"""["']image["']\s*:\s*(?:\[\s*)?["'](https?://[^"']+)["']""", page, re.I)
        if match:
            candidate = html.unescape(match.group(1).strip())
    if not candidate:
        return None
    candidate = urllib.parse.urljoin(url, candidate)
    image_parts = urllib.parse.urlsplit(candidate)
    if image_parts.scheme != "https" or not image_parts.netloc:
        return None
    if any(x in candidate.lower() for x in ("logo", "avatar", "favicon", "icon-", "sprite")):
        return None
    return {
        "image_url": candidate,
        "image_alt": post.get("title", "")[:250],
        "image_source_url": url,
    }

missing = [p for p in posts if not p.get("image_url")]
missing.sort(key=lambda p: p.get("published_at", ""), reverse=True)
# First try the exact publisher page for the newest stories. This is usually
# the most contextually accurate image, even when external image indexes miss it.
publisher_batch = missing[:350]
print(f"Extraindo imagens das páginas originais: {len(publisher_batch)} matérias recentes.", flush=True)
resolved_publisher = 0
with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {pool.submit(publisher_image_lookup, post): post for post in publisher_batch}
    for future in as_completed(futures):
        post = futures[future]
        try:
            match = future.result()
        except Exception:
            match = None
        if match:
            post.update(match)
            resolved_publisher += 1
print(f"Imagens encontradas nas páginas originais: {resolved_publisher}/{len(publisher_batch)}", flush=True)
missing = [p for p in posts if not p.get("image_url")]
print(f"Busca Openverse: {len(missing)} posts ainda sem imagem; até 3 consultas simultâneas.", flush=True)
resolved_openverse = 0
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {pool.submit(openverse_lookup, post): post for post in missing}
    for future in as_completed(futures):
        post = futures[future]
        try:
            match = future.result()
        except Exception:
            match = None
        if match:
            post.update(match)
            resolved_openverse += 1
        if resolved_openverse and resolved_openverse % 25 == 0:
            print(f"Imagens associadas via Openverse: {resolved_openverse}", flush=True)
print(f"Imagens encontradas via Openverse: {resolved_openverse}/{len(missing)}", flush=True)

missing_after_openverse = [p for p in posts if not p.get("image_url")]
print(f"Busca secundária no Wikimedia Commons: {len(missing_after_openverse)} posts ainda sem imagem.", flush=True)
resolved_commons = 0
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {pool.submit(commons_lookup, post): post for post in missing_after_openverse}
    for future in as_completed(futures):
        post = futures[future]
        try:
            match = future.result()
        except Exception:
            match = None
        if match:
            post.update(match)
            resolved_commons += 1
        if resolved_commons and resolved_commons % 25 == 0:
            print(f"Imagens associadas via Wikimedia Commons: {resolved_commons}", flush=True)
print(f"Imagens encontradas via Wikimedia Commons: {resolved_commons}/{len(missing_after_openverse)}", flush=True)

posts.sort(key=lambda item: (item["published_at"], item["url"]), reverse=True)
source_post_counts = {}
for item in posts:
    source_id = item.get("id", "").split("-", 1)[0]
    source_post_counts[source_id] = source_post_counts.get(source_id, 0) + 1

payload = {
    "version": 1,
    "generated_at": now.isoformat(),
    "time_window_days": 90,
    "sort_order": "chronological_descending",
    "configured_source_count": len(sources),
    "source_count_with_posts": len(source_post_counts),
    "source_post_counts": source_post_counts,
    "post_count": len(posts),
    "posts": posts
}
out = ROOT / "data/posts.json"
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
posts_with_images = sum(1 for item in posts if item.get("image_url"))
print(
    f"Posts recentes gerados: {len(posts)}; com imagem: {posts_with_images}; "
    f"sem imagem: {len(posts) - posts_with_images}; fontes configuradas: {len(sources)}; "
    f"fontes representadas: {len(source_post_counts)}; janela: {cutoff.date()} a {now.date()}"
)
