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
        "image_url": None
    })

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
print(
    f"Posts recentes gerados: {len(posts)}; fontes configuradas: {len(sources)}; "
    f"fontes representadas: {len(source_post_counts)}; janela: {cutoff.date()} a {now.date()}"
)
