#!/usr/bin/env python3
"""Generate original Portuguese editorial stories using Gemini or OpenAI API.

Prefers GEMINI_API_KEY; falls back to OPENAI_API_KEY. The script creates original
pieces with explicit source attribution and never claims independent corroboration
when only one source has been consulted.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "data" / "discovered-articles.json"
OUTPUT_PATH = ROOT / "data" / "editorial-posts.json"
STATUS_PATH = ROOT / "data" / "editorial-pipeline-status.json"
ARTICLES_DIR = ROOT / "artigos"
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta/models")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_PER_RUN = max(1, min(int(os.getenv("MAX_ARTICLES_PER_RUN", "5")), 10))
TIMEOUT = 45
USER_AGENT = "EnigmaMundiEditorialBot/1.0 (+https://github.com/sites84/Enigma-mundi)"
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "button", "noscript", "svg"}
CATEGORY_LABELS = {
    "ufologia": "Ufologia",
    "conspiracoes": "Teorias da conspiração",
    "criptozoologia_lendas": "Lendas e fenômenos incomuns",
    "fact_checking": "Investigação e checagem",
    "arquivos_oficiais": "Arquivos oficiais",
    "misterios": "Mistérios",
}


class ArticleTextParser(HTMLParser):
    """Extract article-like text without retaining page markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.skip_tags = set()
        self.in_article = 0
        self.in_main = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_tags.add(tag)
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "article":
            self.in_article += 1
        elif tag == "main":
            self.in_main += 1
        elif tag in {"p", "h1", "h2", "h3", "h4", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.skip_tags:
            self.skip_tags.remove(tag)
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "article":
            self.in_article = max(0, self.in_article - 1)
        elif tag == "main":
            self.in_main = max(0, self.in_main - 1)
        elif tag in {"p", "h1", "h2", "h3", "h4", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value + " ")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value):
    value = value.lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    return value[:72] or "reportagem"


def canonical_url(value):
    try:
        p = urllib.parse.urlsplit(str(value or "").strip())
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref", "source"}
             and not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urllib.parse.urlencode(sorted(q)), ""))
    except (TypeError, ValueError):
        return ""


def fetch_article(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(2_000_000).decode("utf-8", "replace")
    if "html" not in content_type.lower() and "<html" not in raw[:1000].lower():
        raise ValueError("A URL não retornou uma página HTML")
    parser = ArticleTextParser()
    parser.feed(raw)
    text = re.sub(r"[ \t]+\n", "\n", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Avoid sending an entire book or the page's navigation boilerplate to the model.
    return final_url, text[:18000]


def call_model(api_key, provider, model, title, description, source_name, source_url, source_text, category):
    system = (
        "Você é editor de um portal jornalístico brasileiro chamado Enigma Mundi, especializado em mistérios, "
        "ufologia, fenômenos incomuns, folclore, ciência e arquivos históricos. Produza uma reportagem ORIGINAL "
        "em português brasileiro a partir dos fatos e alegações disponíveis. Não traduza parágrafo a parágrafo, "
        "não imite a estrutura da fonte e não reutilize frases características. Não invente datas, nomes, números, "
        "citações, documentos, especialistas, evidências ou fontes. Separe claramente fatos relatados, alegações e "
        "hipóteses. Se houver apenas uma fonte, diga isso de forma transparente e não afirme que houve confirmação "
        "independente. Não transforme especulação em fato. Evite clickbait enganoso. Não use citações diretas. "
        "A seção 'O que pode ser?' deve apresentar hipóteses plausíveis e limites da evidência, sem inventar uma "
        "resposta definitiva. Gere conteúdo substancial e legível, com ângulo e organização próprios."
    )
    user = {
        "task": "Criar uma reportagem editorial original para publicação no site.",
        "source": {"name": source_name, "url": source_url, "original_title": title,
                   "description": description, "category_hint": category},
        "source_page_text": source_text,
        "output_language": "pt-BR",
        "requirements": [
            "Título de 45 a 95 caracteres, informativo e sem sensacionalismo.",
            "Resumo de 120 a 200 caracteres.",
            "Abertura de 2 a 3 parágrafos, explicando o que aconteceu e por que importa.",
            "Entre 3 e 5 seções, cada uma com heading e 2 a 4 parágrafos substantivos.",
            "Uma seção separada 'O que pode ser?' com hipóteses, evidências e limitações.",
            "Conclusão que informe o que se sabe e o que permanece sem resposta.",
            "Não copiar a redação da fonte. Não inventar fontes adicionais.",
            "Use apenas a fonte fornecida em source_references. Não alegue que outras fontes foram consultadas.",
            "Categoria deve ser uma destas: ufologia, conspiracoes, criptozoologia_lendas, fact_checking, arquivos_oficiais, misterios.",
            "Retorne JSON válido com as chaves title, excerpt, category, lead (array de parágrafos), sections (array de objetos heading e paragraphs), what_might_it_be (array de parágrafos), conclusion (array de parágrafos), tags (array de 3 a 6 strings)."
        ],
        "source_references": [{"name": source_name, "url": source_url}],
    }
    if provider == "gemini":
        endpoint = f"{GEMINI_API_BASE}/{urllib.parse.quote(model, safe='')}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(user, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        }
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
    else:
        endpoint = OPENAI_API_URL
        payload = {
            "model": model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(3000).decode("utf-8", "replace")
        raise RuntimeError(f"API {provider} retornou HTTP {exc.code}: {detail[:500]}") from exc
    if provider == "gemini":
        candidates = result.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts", []) if candidates else [])
        content = next((part.get("text", "") for part in parts if part.get("text")), "")
    else:
        choices = result.get("choices") or []
        content = choices[0].get("message", {}).get("content", "") if choices else ""
    if not content:
        raise RuntimeError(f"A API {provider} não retornou conteúdo")
    try:
        article = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"A resposta da API {provider} não contém JSON válido") from exc
    return article


def call_model_with_fallback(api_key, provider, model, title, description, source_name, source_url, source_text, category):
    """Try other API-supported Flash model IDs when the selected model is busy or unavailable."""
    models = [model]
    if provider == "gemini":
        models.extend([
            "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
            "gemini-3.5-flash-lite", "gemini-3-flash-preview",
            "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash",
        ])
    errors = []
    for candidate in dict.fromkeys(models):
        try:
            result = call_model(api_key, provider, candidate, title, description,
                                source_name, source_url, source_text, category)
            if candidate != model:
                print(f"[INFO] Geração concluída usando modelo alternativo '{candidate}'.", flush=True)
            return result, candidate
        except RuntimeError as exc:
            message = str(exc)
            if provider != "gemini" or not re.search(r"HTTP (?:404|429|500|503)", message):
                raise
            errors.append(f"{candidate}: {message[:180]}")
            print(f"[WARN] Modelo '{candidate}' indisponível/ocupado; tentando alternativa.", flush=True)
    raise RuntimeError("Nenhum modelo Gemini alternativo respondeu. " + " | ".join(errors[-4:]))


def clean_paragraphs(value, minimum=1):
    if not isinstance(value, list):
        return []
    return [re.sub(r"\s+", " ", str(item)).strip() for item in value
            if isinstance(item, str) and len(item.strip()) >= 45][:8]


def validate_article(article):
    required = ("title", "excerpt", "category", "lead", "sections", "what_might_it_be", "conclusion")
    if not isinstance(article, dict) or any(key not in article for key in required):
        raise ValueError("A reportagem gerada não contém todos os campos obrigatórios")
    article["title"] = re.sub(r"\s+", " ", str(article["title"])).strip()[:150]
    article["excerpt"] = re.sub(r"\s+", " ", str(article["excerpt"])).strip()[:260]
    article["lead"] = clean_paragraphs(article["lead"])
    article["what_might_it_be"] = clean_paragraphs(article["what_might_it_be"])
    article["conclusion"] = clean_paragraphs(article["conclusion"])
    article["sections"] = [
        {"heading": re.sub(r"\s+", " ", str(section.get("heading", ""))).strip()[:120],
         "paragraphs": clean_paragraphs(section.get("paragraphs"))}
        for section in article["sections"] if isinstance(section, dict)
    ][:5]
    article["sections"] = [s for s in article["sections"] if s["heading"] and len(s["paragraphs"]) >= 2]
    if len(article["title"]) < 30 or len(article["excerpt"]) < 90:
        raise ValueError("Título ou resumo curto demais")
    if len(article["lead"]) < 2 or len(article["sections"]) < 3:
        raise ValueError("Reportagem curta: exige abertura e pelo menos três seções")
    if not article["what_might_it_be"] or not article["conclusion"]:
        raise ValueError("Faltam a seção de hipóteses ou a conclusão")
    article["category"] = article["category"] if article["category"] in CATEGORY_LABELS else "misterios"
    tags = article.get("tags", [])
    article["tags"] = [str(t).strip()[:40] for t in tags if str(t).strip()][:6] if isinstance(tags, list) else []
    return article


def esc(value):
    return html.escape(str(value or ""), quote=True)


def paragraphs(items):
    return "\n".join(f"<p>{esc(p)}</p>" for p in items)


def render_article_page(article, post, slug):
    page_title = esc(article["title"])
    excerpt = esc(article["excerpt"])
    site_url = "https://sites84.github.io/Enigma-mundi/"
    page_url = site_url + "artigos/" + slug + ".html"
    image = post.get("image_url")
    image_html = (f'<figure class="article-cover"><img src="{esc(image)}" alt="{esc(post.get("image_alt") or article["title"])}" loading="eager"><figcaption>Imagem relacionada ao tema. <a href="{esc(post["source_url"])}" rel="noopener noreferrer">Fonte da matéria</a></figcaption></figure>') if image else ""
    sections_html = "\n".join(
        f"<section><h2>{esc(section['heading'])}</h2>{paragraphs(section['paragraphs'])}</section>"
        for section in article["sections"]
    )
    source_name = esc(post["primary_source_name"])
    source_url = esc(post["source_url"])
    tags = ", ".join(article["tags"])
    return f'''<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title} | Enigma Mundi</title>
<meta name="description" content="{excerpt}">
<link rel="canonical" href="{esc(page_url)}">
<meta property="og:type" content="article"><meta property="og:title" content="{page_title}">
<meta property="og:description" content="{excerpt}"><meta property="og:url" content="{esc(page_url)}">
{f'<meta property="og:image" content="{esc(image)}">' if image else ''}
<link rel="stylesheet" href="../assets/styles.css">
<style>
.article-page{{max-width:850px;margin:0 auto;padding:44px 22px 80px;color:var(--text,#ece9e2)}}
.article-page .article-back{{display:inline-block;margin-bottom:28px;color:inherit}}
.article-page .article-kicker{{text-transform:uppercase;letter-spacing:.12em;font-size:.78rem;opacity:.75}}
.article-page h1{{font-size:clamp(2rem,5vw,3.8rem);line-height:1.12;margin:.7rem 0 1rem}}
.article-page .article-deck{{font-size:1.2rem;line-height:1.7;opacity:.88}}
.article-page .article-meta{{font-size:.88rem;opacity:.7;margin:20px 0 28px}}
.article-page .article-cover img{{width:100%;max-height:520px;object-fit:cover;border-radius:14px}}
.article-page figcaption{{font-size:.8rem;opacity:.7;margin-top:8px}}
.article-page .article-body{{font-size:1.06rem;line-height:1.85}}
.article-page .article-body h2{{font-size:1.65rem;line-height:1.25;margin:2.4rem 0 .8rem}}
.article-page .article-body p{{margin:0 0 1.1rem}}
.article-page .source-box{{margin-top:42px;padding:20px;border:1px solid #555;border-radius:12px;overflow-wrap:anywhere}}
.article-page a{{color:inherit}}
</style>
<script type="application/ld+json">{json.dumps({"@context":"https://schema.org","@type":"NewsArticle","headline":article["title"],"description":article["excerpt"],"datePublished":post["published_at"],"dateModified":post["generated_at"],"author":{"@type":"Organization","name":"Enigma Mundi"},"publisher":{"@type":"Organization","name":"Enigma Mundi"},"mainEntityOfPage":page_url,"image":[image] if image else [],"isBasedOn":[post["source_url"]]},ensure_ascii=False).replace("</","<\\/")}</script>
</head>
<body>
<div class="topline"><span>JORNALISMO DE INVESTIGAÇÃO • MISTÉRIOS SEM FRONTEIRAS</span><span>ENIGMA MUNDI</span></div>
<header class="site-header"><a class="brand" href="../" aria-label="Enigma Mundi, página inicial"><span class="brand-mark" aria-hidden="true">E<span>M</span></span><span class="brand-copy"><strong>ENIGMA MUNDI</strong><small>O mundo ainda guarda segredos</small></span></a><nav aria-label="Navegação principal"><a href="../">Início</a><a href="../#ultimas">Últimas matérias</a></nav></header>
<main class="article-page">
<a class="article-back" href="../#ultimas">← Voltar às matérias</a>
<div class="article-kicker">{esc(CATEGORY_LABELS[article["category"]])} · Reportagem original</div>
<h1>{page_title}</h1><p class="article-deck">{excerpt}</p>
<div class="article-meta">Enigma Mundi · Atualizado em {esc(post["generated_at"][:10])}</div>
{image_html}
<article class="article-body">
{paragraphs(article["lead"])}
{sections_html}
<section><h2>O que pode ser?</h2>{paragraphs(article["what_might_it_be"])}</section>
<section><h2>Conclusão</h2>{paragraphs(article["conclusion"])}</section>
</article>
<div class="source-box"><strong>Fonte consultada</strong><p>Esta reportagem foi produzida a partir das informações publicadas por {source_name}. A existência de uma publicação não significa, por si só, confirmação independente de suas alegações.</p><p><a href="{source_url}" target="_blank" rel="noopener noreferrer">Consultar a publicação original ↗</a></p>{f'<p>Temas: {esc(tags)}</p>' if tags else ''}</div>
</main>
<footer class="site-footer"><a class="footer-brand" href="../">ENIGMA MUNDI</a><span>Curiosidade, apuração e limites da evidência.</span><span>© Enigma Mundi</span></footer>
</body></html>
'''


def resolve_gemini_model(api_key, preferred):
    """Choose a model that this exact API key can access and that supports generateContent."""
    request = urllib.request.Request(
        GEMINI_API_BASE,
        headers={"x-goog-api-key": api_key, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1500).decode("utf-8", "replace")
        raise RuntimeError(f"Não foi possível listar modelos Gemini (HTTP {exc.code}): {detail[:300]}") from exc
    models = []
    for item in payload.get("models", []):
        methods = item.get("supportedGenerationMethods", [])
        name = str(item.get("name", "")).removeprefix("models/")
        if name and "generateContent" in methods:
            models.append(name)
    if not models:
        raise RuntimeError("A API Gemini não listou modelos compatíveis com generateContent para esta chave.")
    if preferred in models:
        return preferred
    priorities = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                  "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3-flash-preview",
                  "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash")
    for name in priorities:
        if name in models:
            print(f"[INFO] Modelo configurado '{preferred}' indisponível para esta chave; usando '{name}'.", flush=True)
            return name
    # Prefer a Flash model returned by the API, without selecting embedding/audio/image-only models.
    flash_models = [name for name in models if "flash" in name.lower() and not any(
        marker in name.lower() for marker in ("embedding", "image", "tts", "audio", "live"))]
    if flash_models:
        print(f"[INFO] Usando modelo Flash disponível: '{flash_models[0]}'.", flush=True)
        return flash_models[0]
    raise RuntimeError("A chave Gemini não tem nenhum modelo Flash de geração de texto disponível. Verifique o projeto e a API habilitada.")


def main():
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if gemini_key:
        api_key, provider, model = gemini_key, "gemini", GEMINI_MODEL
        try:
            model = resolve_gemini_model(api_key, model)
        except Exception as exc:
            status = {
                "updated_at": now_iso(), "provider": "gemini", "model": None,
                "existing_articles": 0, "generated_this_run": 0,
                "status": "errors", "errors": [{"error": str(exc)[:500]}],
            }
            write_json(STATUS_PATH, status)
            print(f"[ERROR] {str(exc)[:500]}", flush=True)
            return 0
    elif openai_key:
        api_key, provider, model = openai_key, "openai", OPENAI_MODEL
    else:
        api_key, provider, model = "", "", ""
    existing_payload = read_json(OUTPUT_PATH, {"version": 1, "articles": []})
    existing_articles = existing_payload.get("articles", []) if isinstance(existing_payload, dict) else []
    if not isinstance(existing_articles, list):
        existing_articles = []
    existing_urls = {canonical_url(a.get("source_url")) for a in existing_articles if a.get("source_url")}
    status = {
        "updated_at": now_iso(), "provider": provider or None, "model": model or None,
        "existing_articles": len(existing_articles), "generated_this_run": 0,
        "status": "waiting_for_api_key", "errors": [],
    }
    if not api_key:
        status["message"] = "Configure GEMINI_API_KEY (recomendado) ou OPENAI_API_KEY em Settings > Secrets and variables > Actions para ativar a geração editorial."
        write_json(STATUS_PATH, status)
        print("Nenhuma chave GEMINI_API_KEY/OPENAI_API_KEY configurada. A fila foi preservada; nenhum conteúdo foi inventado ou publicado.")
        return 0

    queue = read_json(QUEUE_PATH, {"articles": []})
    articles = queue.get("articles", []) if isinstance(queue, dict) else []
    candidates = []
    for item in articles:
        url = canonical_url(item.get("url"))
        if not url or url in existing_urls or item.get("status") != "discovered":
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if len(title) < 18 or len(description) < 35:
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: (item.get("published_at") or "", item.get("discovered_at") or ""), reverse=True)
    status["status"] = "running"
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0
    for item in candidates[:MAX_PER_RUN]:
        url = canonical_url(item.get("url"))
        try:
            final_url, source_text = fetch_article(url)
            if len(source_text) < 900:
                raise ValueError(f"Conteúdo extraído insuficiente ({len(source_text)} caracteres); não gerar artigo a partir apenas de título/resumo")
            category = item.get("category") or "misterios"
            source_name = item.get("source_name") or item.get("source_id") or "Fonte externa"
            draft, used_model = call_model_with_fallback(api_key, provider, model, item["title"], item.get("description", ""), source_name, final_url, source_text, category)
            status["model"] = used_model
            article = validate_article(draft)
            fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
            slug = slugify(article["title"])[:60] + "-" + fingerprint
            output_path = ARTICLES_DIR / f"{slug}.html"
            if output_path.exists():
                existing_urls.add(url)
                continue
            published_at = item.get("published_at") or now_iso()
            record = {
                "id": "em-" + fingerprint,
                "title": article["title"],
                "excerpt": article["excerpt"],
                "category": article["category"],
                "category_label": CATEGORY_LABELS[article["category"]],
                "published_at": published_at,
                "generated_at": now_iso(),
                "source_name": "Enigma Mundi",
                "primary_source_name": source_name,
                "source_url": final_url,
                "url": final_url,
                "path": "artigos/" + output_path.name,
                "status": "published",
                "post_type": "original_article",
                "image_url": item.get("image_url"),
                "image_source_url": final_url if item.get("image_url") else None,
                "image_alt": article["title"] if item.get("image_url") else None,
                "tags": article["tags"],
            }
            output_path.write_text(render_article_page(article, record, slug), encoding="utf-8")
            existing_articles.append(record)
            existing_urls.add(url)
            generated += 1
            print(f"[OK] Artigo criado: {record['path']} — {record['title']}", flush=True)
            time.sleep(1)
        except Exception as exc:
            message = f"{item.get('source_name', item.get('source_id', 'fonte'))}: {str(exc)[:400]}"
            status["errors"].append({"url": url, "error": message})
            print(f"[WARN] {message}", flush=True)
    existing_articles.sort(key=lambda a: (a.get("published_at", ""), a.get("generated_at", "")), reverse=True)
    write_json(OUTPUT_PATH, {
        "version": 1, "generated_at": now_iso(),
        "article_count": len(existing_articles), "articles": existing_articles,
    })
    status.update({
        "updated_at": now_iso(),
        "status": "completed" if generated else ("errors" if status["errors"] else "no_new_candidates"),
        "existing_articles": len(existing_articles),
        "generated_this_run": generated,
        "candidate_count": len(candidates),
    })
    write_json(STATUS_PATH, status)
    print(f"Artigos novos: {generated}; total publicado: {len(existing_articles)}; erros: {len(status['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
