#!/usr/bin/env python3
"""Idempotently sync Enigma Mundi JSON editorial data to Supabase.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the process environment.
Never place the service-role key in repository files or browser code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSTS_PATH = ROOT / "data" / "editorial-posts.json"
QUEUE_PATH = ROOT / "data" / "discovered-articles.json"
STATUS_PATH = ROOT / "data" / "editorial-pipeline-status.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc


def as_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("articles", "items", "results", "sources"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def request_upsert(base_url, key, table, rows, conflict):
    if not rows:
        return 0
    endpoint = base_url.rstrip("/") + "/rest/v1/" + table + "?" + urllib.parse.urlencode({"on_conflict": conflict})
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status not in (200, 201, 204):
                raise RuntimeError(f"Supabase {table} upsert returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"Supabase {table} upsert failed (HTTP {exc.code}): {detail}") from exc
    return len(rows)


def main():
    base_url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not base_url or not key:
        print("Supabase sync skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    posts_payload = load_json(POSTS_PATH, {})
    queue_payload = load_json(QUEUE_PATH, [])
    status = load_json(STATUS_PATH, {})
    posts = as_items(posts_payload)
    queue = as_items(queue_payload)

    discovered_rows = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        url = item.get("source_url") or item.get("url") or item.get("canonical_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        discovered_rows.append({
            "canonical_url": url,
            "source_id": str(item.get("source_id") or item.get("source") or "") or None,
            "source_name": item.get("source_name") or item.get("source"),
            "title": item.get("title"),
            "description": item.get("description") or item.get("excerpt"),
            "category": item.get("category"),
            "published_at": item.get("published_at") or None,
            "image_url": item.get("image_url"),
            "image_source_url": item.get("image_source_url"),
            "image_alt": item.get("image_alt"),
            "metadata": item,
            "processing_status": "generated" if url in {p.get("source_url") or p.get("url") for p in posts if isinstance(p, dict)} else "pending",
            "updated_at": now,
        })

    article_rows = []
    for item in posts:
        if not isinstance(item, dict):
            continue
        source_url = item.get("source_url") or item.get("url")
        article_id = item.get("id")
        title = item.get("title")
        excerpt = item.get("excerpt")
        if not all(isinstance(v, str) and v for v in (source_url, article_id, title, excerpt)):
            continue
        article_rows.append({
            "id": article_id,
            "title": title,
            "slug": item.get("slug"),
            "excerpt": excerpt,
            "category": item.get("category"),
            "category_label": item.get("category_label"),
            "body_markdown": item.get("body_markdown") or item.get("content"),
            "html_path": item.get("path"),
            "source_name": item.get("primary_source_name") or item.get("source_name"),
            "source_url": source_url,
            "image_url": item.get("image_url"),
            "image_source_url": item.get("image_source_url"),
            "image_alt": item.get("image_alt"),
            "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            "status": item.get("status") if item.get("status") in ("draft", "published", "archived") else "draft",
            "generated_at": item.get("generated_at") or now,
            "published_at": item.get("published_at") or None,
            "metadata": item,
            "updated_at": now,
        })

    discovered_count = request_upsert(base_url, key, "discovered_articles", discovered_rows, "canonical_url")
    article_count = request_upsert(base_url, key, "editorial_articles", article_rows, "source_url")
    print(f"Supabase sync complete: {discovered_count} discovered-source records and {article_count} editorial articles upserted.")
    print(f"Local pipeline status: {status.get('status', 'unknown')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Supabase editorial sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
