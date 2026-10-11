#!/usr/bin/env python3
"""Build the site's dated 90-day source-curation list from the discovery queue."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

posts.sort(key=lambda item: (item["published_at"], item["url"]))
payload = {
    "version": 1,
    "generated_at": now.isoformat(),
    "time_window_days": 90,
    "sort_order": "chronological_ascending",
    "post_count": len(posts),
    "posts": posts
}
out = ROOT / "data/posts.json"
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Posts recentes gerados: {len(posts)}; janela: {cutoff.date()} a {now.date()}")
