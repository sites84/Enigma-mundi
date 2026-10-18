#!/usr/bin/env python3
"""Generate original Portuguese editorial stories with Gemini only.

Source extraction can fall back to an RSS description when the publisher blocks
automated reading, while preserving strict topic and evidence constraints.
Articles must pass full structure and editorial relevance checks before publication.
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

GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta/models")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
MAX_PER_RUN = max(1, min(int(os.getenv("MAX_ARTICLES_PER_RUN", "3")), 3))
TIMEOUT = 45
SOURCE_TIMEOUT = 12  # Match Tech Check: do not let a single publisher block a run.
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
    # Publisher feeds sometimes return short-lived 429/5xx responses or a
    # transient timeout. Retry the source fetch once, with a bounded delay;
    # never loop indefinitely or bypass the minimum-body quality gate.
    for fetch_attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(2_000_000).decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code in (500, 502, 503, 504)
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            if fetch_attempt == 0 and retryable:
                try:
                    delay = min(8.0, max(2.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = 3.0
                print(f"[INFO] Fonte respondeu HTTP {exc.code}; nova tentativa única em {delay:.0f}s.", flush=True)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if fetch_attempt == 0:
                print("[INFO] Falha temporária ao acessar a fonte; repetindo uma única vez em 2s.", flush=True)
                time.sleep(2)
                continue
            raise
    if "html" not in content_type.lower() and "<html" not in raw[:1000].lower():
        raise ValueError("A URL não retornou uma página HTML")
    parser = ArticleTextParser()
    parser.feed(raw)
    text = re.sub(r"[ \t]+\n", "\n", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Some publishers expose the complete story in JSON-LD while their visible
    # HTML is mostly navigation or a JavaScript shell. Recover articleBody only
    # when the normal page extraction is too short; never use metadata snippets
    # as a substitute for the article itself.
    if len(text) < 900:
        def article_bodies(value):
            found = []
            if isinstance(value, dict):
                body = value.get("articleBody")
                if isinstance(body, str) and len(body.strip()) >= 900:
                    found.append(body.strip())
                for nested in value.values():
                    found.extend(article_bodies(nested))
            elif isinstance(value, list):
                for nested in value:
                    found.extend(article_bodies(nested))
            return found

        scripts = re.findall(
            r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>',
            raw, flags=re.IGNORECASE | re.DOTALL,
        )
        recovered = []
        for script in scripts:
            try:
                recovered.extend(article_bodies(json.loads(html.unescape(script))))
            except (json.JSONDecodeError, TypeError):
                continue
        if recovered:
            recovered.sort(key=len, reverse=True)
            if len(recovered[0]) > len(text):
                text = re.sub(r"\s+", " ", recovered[0]).strip()
                print(f"[INFO] Texto recuperado do articleBody JSON-LD: {len(text)} caracteres.", flush=True)

    # Avoid sending an entire book or the page's navigation boilerplate to the model.
    return final_url, text[:18000]


def parse_json_object(content, provider):
    """Parse a complete JSON object, tolerating code fences or harmless surrounding text."""
    cleaned = str(content or "").strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Find a balanced JSON object without mistaking braces inside quoted strings.
    start = cleaned.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(cleaned)):
            char = cleaned[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:pos + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise RuntimeError(f"A resposta da API {provider} não contém um objeto JSON completo e válido")


def call_model(api_key, provider, model, title, description, source_name, source_url, source_text, category):
    system = (
        "Você é editor de um portal jornalístico brasileiro chamado Enigma Mundi, especializado em mistérios, "
        "ufologia, fenômenos incomuns, folclore, ciência e arquivos históricos. Produza uma reportagem ORIGINAL "
        "em português brasileiro a partir dos fatos e alegações disponíveis. Não traduza parágrafo a parágrafo, "
        "não imite a estrutura da fonte e não reutilize frases características. Não invente datas, nomes, números, "
        "citações, documentos, especialistas, evidências ou fontes. Separe claramente fatos relatados, alegações e "
        "hipóteses. Se houver apenas uma fonte, diga isso de forma transparente e não afirme que houve confirmação "
        "independente. Não transforme especulação em fato. Evite clickbait enganoso. Não use citações diretas. "
        "IMPORTANTE: nunca entregue apenas um resumo, sinopse, nota curta ou paráfrase superficial. Produza uma "
        "reportagem completa, desenvolvida e autossuficiente, reescrita em linguagem original e com organização própria, "
        "cobrindo os fatos relevantes disponíveis no material fornecido. Desenvolva contexto, cronologia quando houver, "
        "protagonistas, evidências citadas, explicações, controvérsias e limitações; não invente detalhes ausentes. "
        "Não traduza nem reproduza a estrutura parágrafo a parágrafo e não substitua o texto original por um resumo. "
        "Mantenha o corpo entre 550 e 700 palavras no total. Cada parágrafo deve ter em média 30 a 45 palavras e 2 a 3 frases. Priorize os fatos relevantes da fonte, sem repetições, e respeite a estrutura solicitada. A resposta final deve ser somente um objeto JSON válido, sem Markdown nem texto externo. A seção 'O que pode ser?' deve apresentar hipóteses plausíveis "
        "e limites da evidência, sem inventar uma resposta definitiva. Gere conteúdo substancial e legível, com ângulo próprio."
    )
    model_source_text = source_text
    if provider == "groq" and len(source_text) > 4200:
        # Bound input tokens so the free-tier TPM limit leaves room for a complete
        # structured report. Preserve the opening and ending of the source.
        model_source_text = source_text[:2400] + "\n\n[... trecho final da matéria original ...]\n\n" + source_text[-800:]
    user = {
        "task": "Criar uma reportagem editorial original para publicação no site.",
        "source": {"name": source_name, "url": source_url, "original_title": title,
                   "description": description, "category_hint": category},
        "source_page_text": model_source_text,
        "output_language": "pt-BR",
        "requirements": [
            "Título de 45 a 95 caracteres, informativo e sem sensacionalismo.",
            "Resumo de 120 a 200 caracteres apenas para o campo excerpt; ele NÃO substitui a reportagem completa.",
            "Escreva uma reportagem completa e objetiva, sem repetição nem introduções genéricas. Use somente fatos presentes no texto-fonte; mantenha o corpo entre 550 e 700 palavras e parágrafos concisos.",
            "Abertura com 3 parágrafos substanciais, cobrindo acontecimento, contexto e importância.",
            "Use exatamente 4 seções temáticas, cada uma com heading e 2 parágrafos concisos. Não repita a abertura.",
            "Uma seção separada 'O que pode ser?' com 2 parágrafos concisos sobre hipóteses, evidências e limitações.",
            "Conclusão com 2 parágrafos concisos que diferenciem fatos conhecidos e questões em aberto.",
            "Não copiar a redação da fonte. Não inventar fontes adicionais.",
            "Use apenas a fonte fornecida em source_references. Não alegue que outras fontes foram consultadas.",
            "Categoria deve ser uma destas: ufologia, conspiracoes, criptozoologia_lendas, fact_checking, arquivos_oficiais, misterios.",
            "Retorne JSON válido com as chaves title, excerpt, category, lead (array de parágrafos), sections (array de objetos heading e paragraphs), what_might_it_be (array de parágrafos), conclusion (array de parágrafos), tags (array de 3 a 6 strings)."
        ],
        "source_references": [{"name": source_name, "url": source_url}],
    }
    serialized_user = json.dumps(user, ensure_ascii=False)
    if provider == "groq":
        endpoint = f"{GROQ_API_BASE}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": serialized_user},
            ],
            "temperature": 0.3,
            "max_completion_tokens": 2200,
            # GPT-OSS spends part of its completion budget on reasoning. Keep it low
            # so the actual article JSON is not cut off on the free tier.
            "reasoning_effort": "low",
            "reasoning_format": "hidden",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "enigma_mundi_article",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "excerpt": {"type": "string"},
                            "category": {"type": "string", "enum": list(CATEGORY_LABELS)},
                            "lead": {"type": "array", "items": {"type": "string"}},
                            "sections": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "heading": {"type": "string"},
                                        "paragraphs": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["heading", "paragraphs"],
                                    "additionalProperties": False,
                                },
                            },
                            "what_might_it_be": {"type": "array", "items": {"type": "string"}},
                            "conclusion": {"type": "array", "items": {"type": "string"}},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title", "excerpt", "category", "lead", "sections",
                            "what_might_it_be", "conclusion", "tags",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
    elif provider == "gemini":
        endpoint = f"{GEMINI_API_BASE}/{urllib.parse.quote(model, safe='')}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": serialized_user}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    else:
        raise RuntimeError(f"Provedor de IA não suportado: {provider}")
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    # Groq's free tier can return a short-lived tokens-per-minute 429 even
    # when the key is valid. Honor its explicit retry delay once, rather than
    # wasting the remaining candidate attempts or requiring a manual rerun.
    for request_attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read(3000).decode("utf-8", "replace")
            is_groq_tpm = (
                provider == "groq" and exc.code == 429
                and "tokens per minute" in detail.lower()
            )
            delay_match = re.search(r"try again in ([0-9.]+)\s*(ms|s)", detail, re.IGNORECASE)
            if request_attempt == 0 and is_groq_tpm:
                if delay_match:
                    amount = float(delay_match.group(1))
                    unit = delay_match.group(2).lower()
                    retry_seconds = amount / 1000.0 if unit == "ms" else amount
                    delay = min(60.0, max(1.0, retry_seconds + 0.5))
                else:
                    # TPM responses can omit a parseable Retry-After value.
                    delay = 2.0
                print(f"[INFO] Limite temporário de tokens/minuto do Groq; aguardando {delay:.1f}s e repetindo uma única vez.", flush=True)
                time.sleep(delay)
                continue
            raise RuntimeError(f"API {provider} retornou HTTP {exc.code}: {detail[:700]}") from exc

    if provider == "groq":
        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError("A API Groq não retornou candidatos de resposta")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason and finish_reason != "stop":
            raise RuntimeError(f"Resposta Groq incompleta (finish_reason={finish_reason}); a reportagem não será publicada para evitar texto cortado")
        content = (choice.get("message") or {}).get("content") or ""
    else:
        candidates = result.get("candidates") or []
        if not candidates:
            raise RuntimeError("A API Gemini não retornou candidatos de resposta")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason != "STOP":
            raise RuntimeError(f"Resposta Gemini incompleta (finishReason={finish_reason}); a reportagem não será publicada para evitar texto cortado")
        parts = candidate.get("content", {}).get("parts", [])
        content = next((part.get("text", "") for part in parts if part.get("text")), "")
    if not content:
        raise RuntimeError(f"A API {provider} não retornou conteúdo")
    return parse_json_object(content, provider)


def call_model_with_fallback(api_key, provider, model, title, description, source_name, source_url, source_text, category):
    """Use Groq's free developer tier when configured; preserve strict article validation."""
    if provider == "groq":
        try:
            draft = call_model(api_key, provider, model, title, description, source_name, source_url, source_text, category)
            return validate_article(draft), model
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            lowered = message.lower()
            if "tokens per minute" in lowered and "http 429" in lowered:
                raise RuntimeError("RATE_LIMITED: limite temporário de tokens por minuto da API Groq; a execução será pausada para não desperdiçar chamadas gratuitas: " + message[:850]) from exc
            # If Groq returns malformed/truncated structured output, use the already
            # configured free Gemini key instead of discarding a viable source.
            # TPM throttling is handled above and deliberately pauses this run.
            gemini_fallback_key = os.getenv("GEMINI_API_KEY", "").strip()
            is_quota_error = ("http 429" in lowered or "rate limit" in lowered or "quota" in lowered
                              or "requests per day" in lowered)
            if gemini_fallback_key and not ("tokens per minute" in lowered and "http 429" in lowered):
                print(f"[WARN] Groq não produziu reportagem válida ({message[:180]}). Tentando Gemini gratuito.", flush=True)
                try:
                    return call_model_with_fallback(
                        gemini_fallback_key, "gemini", GEMINI_MODEL, title, description,
                        source_name, source_url, source_text, category
                    )
                except (RuntimeError, ValueError) as fallback_exc:
                    raise RuntimeError(
                        "Groq falhou e o fallback Gemini também não produziu reportagem válida. "
                        f"Groq: {message[:300]} | Gemini: {str(fallback_exc)[:500]}"
                    ) from fallback_exc
            if is_quota_error:
                raise RuntimeError("QUOTA_EXHAUSTED: limite/cota gratuita da API Groq atingido: " + message[:1000]) from exc
            raise
    if provider != "gemini":
        raise RuntimeError(f"Provedor não suportado: {provider}")
    try:
        request = urllib.request.Request(GEMINI_API_BASE, headers={"x-goog-api-key": api_key, "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            catalog = json.loads(response.read().decode("utf-8"))
        supported = {str(item.get("name", "")).removeprefix("models/") for item in catalog.get("models", [])
                     if "generateContent" in item.get("supportedGenerationMethods", [])}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Não foi possível confirmar os modelos Gemini disponíveis: {str(exc)[:250]}") from exc
    # Prefer the configured model, then try every confirmed text-capable Flash model.
    # The previous implementation tried only three models and abandoned the source
    # after a transient 503 overload, which made scheduled runs look successful while
    # publishing nothing.
    priorities = [
        model, "gemini-2.5-flash", "gemini-2.5-flash-lite",
        "gemini-2.0-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash",
        "gemini-3.1-flash-lite", "gemini-3-flash-preview",
    ]
    discovered_flash = sorted(
        name for name in supported
        if "flash" in name.lower()
        and not any(marker in name.lower() for marker in ("embedding", "image", "tts", "audio", "live"))
    )
    models = list(dict.fromkeys(name for name in priorities if name in supported))
    models.extend(name for name in discovered_flash if name not in models)
    # Bound the worst-case runtime: the workflow has a 20-minute job timeout.
    models = models[:5]
    if not models:
        raise RuntimeError("A API não confirmou nenhum dos modelos Gemini Flash de geração priorizados.")
    errors = []
    for candidate in models:
        # 503 is commonly a temporary model-capacity problem. Retry that same
        # model twice before moving to the next available model; do not retry hard quota errors.
        for attempt in range(3):
            try:
                draft = call_model(api_key, "gemini", candidate, title, description, source_name, source_url, source_text, category)
                article = validate_article(draft)
                if candidate != model:
                    print(f"[INFO] Reportagem completa validada com modelo alternativo '{candidate}'.", flush=True)
                return article, candidate
            except (RuntimeError, ValueError) as exc:
                message = str(exc)
                lowered = message.lower()
                if any(marker in lowered for marker in (
                    "exceeded your current quota", "check your plan and billing",
                    "insufficient_quota", "quota exceeded"
                )):
                    raise RuntimeError("QUOTA_EXHAUSTED: " + message[:1200])
                transient_capacity = "http 503" in lowered or "high demand" in lowered or "currently experiencing high demand" in lowered
                if transient_capacity and attempt < 2:
                    delay = 3 * (attempt + 1)
                    print(f"[WARN] Gemini '{candidate}' com sobrecarga temporária; nova tentativa em {delay}s ({attempt + 2}/3).", flush=True)
                    time.sleep(delay)
                    continue
                errors.append(f"{candidate}: {message[:180]}")
                retryable = bool(re.search(r"HTTP (?:429|500|503)", message)) or any(marker in lowered for marker in (
                    "não contém json válido", "não retornou um objeto json", "não retornou conteúdo",
                    "não retornou candidatos", "resposta gemini incompleta", "reportagem incompleta",
                    "reportagem aparentemente truncada", "não contém todos os campos obrigatórios",
                    "título ou resumo curto demais"
                ))
                if not retryable:
                    # Invalid source/content is not repaired by switching models.
                    break
                break
            break
        print(f"[WARN] Modelo Gemini '{candidate}' falhou; tentando o próximo modelo compatível.", flush=True)
    raise RuntimeError("Nenhum dos modelos Gemini disponíveis produziu uma reportagem válida. " + " | ".join(errors))

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
    # Repair an undersized summary from the model's own opening paragraphs rather
    # than spending another API call. Never invent facts or weaken title validation.
    if len(article["excerpt"]) < 120 and article["lead"]:
        lead_text = re.sub(r"\s+", " ", " ".join(article["lead"])).strip()
        sentences = re.split(r"(?<=[.!?])\s+", lead_text)
        summary_parts = []
        for sentence in sentences:
            proposed = " ".join(summary_parts + [sentence]).strip()
            if len(proposed) > 200:
                break
            summary_parts.append(sentence)
            if len(" ".join(summary_parts)) >= 120:
                break
        summary = " ".join(summary_parts).strip()
        if len(summary) < 120:
            summary = lead_text[:200].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        if len(summary) >= 90:
            article["excerpt"] = summary[:260]
    if len(article["title"]) < 30 or len(article["excerpt"]) < 90:
        raise ValueError("Título ou resumo curto demais")
    if len(article["lead"]) < 3 or len(article["sections"]) < 4:
        raise ValueError("Reportagem incompleta: exige três parágrafos de abertura e pelo menos quatro seções desenvolvidas")
    if not article["what_might_it_be"] or not article["conclusion"]:
        raise ValueError("Faltam a seção de hipóteses ou a conclusão")
    # Reject visibly cut-off final paragraphs without imposing any word-count limit.
    final_paragraphs = [article["lead"][-1]]
    final_paragraphs.extend(section["paragraphs"][-1] for section in article["sections"])
    final_paragraphs.extend([article["what_might_it_be"][-1], article["conclusion"][-1]])
    if any(not re.search(r'[.!?…]["”’»)]?\s*$', paragraph.strip()) for paragraph in final_paragraphs):
        raise ValueError("Reportagem aparentemente truncada: parágrafo final sem pontuação de encerramento")
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
    image_html = (f'<figure class="article-cover"><img src="{esc(image)}" alt="{esc(post.get("image_alt") or article["title"])}" loading="eager" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="article-image-fallback" hidden>ENIGMA MUNDI · ARQUIVO VISUAL</div><figcaption>Imagem relacionada ao tema. <a href="{esc(post["source_url"])}" rel="noopener noreferrer">Fonte da matéria</a></figcaption></figure>') if image else ""
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
.article-page .article-cover img{{width:100%;max-height:520px;object-fit:cover;border-radius:14px}}\n.article-page .article-image-fallback{{min-height:220px;display:grid;place-items:center;border:1px solid #49463c;border-radius:14px;background:radial-gradient(ellipse at center,#343126,#171815 72%);color:#c5a66a;letter-spacing:.16em;font-size:.8rem;text-align:center}}\n.article-page .article-image-fallback[hidden]{{display:none}}
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



# Editorial gate: only admit stories directly related to the site's core subjects.
# Broad science, politics, and general news are not eligible just because a feed
# labels them as space, science, official archives, or fact-checking.
# Strict topic gate: only title + feed description can qualify a candidate.
# Never scan the entire article body for generic words like "mystery", "secret" or
# "unexplained": those words occur in ordinary science and political reporting.
CORE_TOPIC_PATTERNS = [
    r"\b(ufo|ufos|ovni|ovnis|uap|uas|fen[oô]meno[s]? a[eé]reo[s]? n[aã]o identificado[s]?)\b",
    r"\b(vida extraterrestre|intelig[eê]ncia extraterrestre|extraterrestre[s]?|alien[s]?|abdu[cç][aã]o|abduction|contato imediato|contato extraterrestre)\b",
    r"\b(criptozoolog\w*|cryptozoolog\w*|bigfoot|sasquatch|yeti|nessie|monstro do lago ness|chupacabra|chupa[- ]?cabra|mokele[- ]?mbembe|pé[- ]?grande)\b",
    r"\b(paranormal|sobrenatural|fantasma[s]?|assombra[cç][aã]o|poltergeist|possess[aã]o|esp[ií]rito[s]?|apari[cç][aã]o fantasmag[oó]rica|haunted|ghost sighting[s]?)\b",
    r"\b(lenda[s]? urbana[s]?|folclore|folklore|criatura[s]? lend[aá]ria[s]?|monstro[s]? lend[aá]rio[s]?|urban legend[s]?|mythical creature[s]?)\b",
    r"\b(teoria[s]? da conspira[cç][aã]o|conspira[cç][oõ]es?|conspiracy theor\w*|conspiracy|sociedade[s]? secreta[s]?|illuminati|nova ordem mundial|new world order|deep state|estado profundo|governo sombra|shadow government|acobertamento[s]?|encobrimento[s]? oficial|cover[- ]?up|ufo cover[- ]?up|documentos desclassificados sobre ovni|opera[cç][aã]o secreta inexplicada|experimento[s]? secreto[s]?|projeto[s]? secreto[s]?|arquivo[s]? secreto[s]?|agenda oculta|manipula[cç][aã]o global)\b",
    r"\b(c[ií]rculos nas planta[cç][oõ]es|crop circles?|artefato[s]? fora do lugar|out[- ]of[- ]place artifact[s]?|oopart|civiliza[cç][aã]o perdida misteriosa|sinal wow|wow signal|fast radio burst inexplicado|sinal de r[aá]dio inexplic[aá]vel)\b",
    r"\b(mist[eé]rio[s]? n[aã]o resolvido[s]?|unsolved mysteries|fen[oô]meno[s]? inexplic[aá]vel|fen[oô]meno[s]? paranormal|anomalia[s]? inexplic[aá]vel|mysterious disappearance[s]?|desaparecimento[s]? inexplic[aá]vel)\b",
]
CORE_TOPIC_RE = re.compile("|".join(f"(?:{p})" for p in CORE_TOPIC_PATTERNS), re.IGNORECASE)


def is_core_topic(title, description="", source_text=""):
    """Accept explicit paranormal/ufology/cryptid signals; reject generic entertainment news."""
    headline = re.sub(r"\s+", " ", str(title or "")).strip()
    summary = re.sub(r"\s+", " ", str(description or "")).strip()
    if not headline:
        return False
    candidate = f"{headline} {summary}"
    # Hard exclusion: election-result and vote-counting coverage is outside this site's remit,
    # even if the feed/category incorrectly labels it as fact-checking or mystery content.
    election_coverage = re.compile(
        r"\b(apura[cç][aã]o|resultado[s]?|candidatura[s]?|candidato[s]?|disputa|corrida)\b.{0,100}\b(eleitoral|elei[cç][oõ]es?|deputado[s]?|senador(?:es)?|governador(?:es)?|prefeito[s]?)\b|"
        r"\b(eleitoral|elei[cç][oõ]es?)\b.{0,100}\b(apura[cç][aã]o|resultado[s]?|deputado[s]?|senador(?:es)?|governador(?:es)?|prefeito[s]?)\b|"
        r"\b(resultado[s]? das? elei[cç][oõ]es?|elei[cç][oõ]es? de 20\d{2}.*(?:1º|primeiro|segundo|2º) turno)\b",
        re.IGNORECASE,
    )
    if election_coverage.search(candidate):
        return False
    # Reject generic academic/editorial announcements that mention folklore without
    # describing a specific mystery, paranormal report, cryptid, UFO case or conspiracy.
    strong_mystery_signal = re.compile(
        r"\b(ufo|uap|ovni|extraterrestrial|alien abduction|cryptozoolog\w*|bigfoot|sasquatch|"
        r"yeti|nessie|chupacabra|paranormal|ghost|haunted|haunting|poltergeist|possession|"
        r"urban legend|conspiracy|illuminati|new world order|unexplained|unsolved mystery|"
        r"mysterious disappearance|crop circles|oopart|wow signal|fantasma[s]?|assombra[cç][aã]o|"
        r"mist[eé]rio[s]? n[aã]o resolvido[s]?|fen[oô]meno[s]? inexplic[aá]vel|teoria[s]? da conspira[cç][aã]o)\b",
        re.IGNORECASE,
    )
    generic_folklore_announcement = re.compile(
        r"\b(new issue|latest issue|journal issue|issue of folklore|folklore now out|"
        r"call for papers|academic journal|journal publication|book review|conference proceedings|"
        r"edi[cç][aã]o acad[eê]mica|novo n[uú]mero do peri[oó]dico|chamada para artigos|"
        r"revista acad[eê]mica|peri[oó]dico revisado por pares|pr[eê]mios acad[eê]micos?|"
        r"pesquisa[s]? acad[eê]mica[s]? sobre folclore|bolsa[s]? de pesquisa|"
        r"award shortlist|shortlist announced|award finalists|prize finalists|"
        r"finalistas? (?:do|da|de) (?:pr[eê]mio|pr[eê]mios|concurso)|"
        r"divulga(?:m|do|da) finalistas|abre (?:inscri[cç][oõ]es|premia[cç][aã]o)|"
        r"premia[cç][aã]o para estudos acad[eê]micos|academic award|folklore award)\b",
        re.IGNORECASE,
    )
    if generic_folklore_announcement.search(candidate) and not strong_mystery_signal.search(candidate):
        return False
    # Comic/satirical columns are not factual paranormal reporting.
    if re.search(r"\b(sunday funnies|comic strip|cartoon|cartoons|comic[s]? strip)\b", candidate, re.IGNORECASE):
        return False
    # A standalone "alien" or a horror/film/game title is not evidence of a real mystery.
    entertainment = re.compile(
        r"\b(video game|videogame|videojogo|jogo[s]? eletr[oô]nico[s]?|game[s]? eletr[oô]nico[s]?|gameplay|gaming|trailer|teaser|sequel|franchise|"
        r"box office|hollywood|movie|film|cinema|tv series|television series|"
        r"streaming|netflix|playstation|xbox|nintendo|dlc|expansion pack|"
        r"alien: isolation|resident evil|silent hill|clayface|comic book)\b",
        re.IGNORECASE,
    )
    if entertainment.search(candidate):
        # Permit entertainment pieces only if their main subject is explicitly a real-world
        # paranormal case, UFO sighting, cryptid report or documented legend.
        # Entertainment terms are a hard rejection unless the headline names a
        # concrete real-world report/case; merely inserting "UFO" into a game title
        # must not qualify the article.
        explicit_case = re.compile(
            r"\b(avistamento[s]?|abdu[cç][aã]o|contato imediato|caso real|caso paranormal|"
            r"investiga[cç][aã]o paranormal|relato[s]? de testemunha[s]?|"
            r"documento[s]? desclassificado[s]?|relat[oó]rio[s]? oficial[ais]?|"
            r"desaparecimento[s]? inexplic[aá]vel|criptozoolog\w*|bigfoot|sasquatch|"
            r"yeti|nessie|chupacabra|fantasma[s]?|assombra[cç][aã]o|poltergeist)\b",
            re.IGNORECASE,
        )
        if not explicit_case.search(headline):
            return False
    # "alien" alone is too broad (often entertainment); extraterrestrial must be contextual.
    generic_alien = re.search(r"\b(aliens?|extraterrestre[s]?)\b", headline, re.IGNORECASE)
    contextual_extraterrestrial = re.search(
        r"\b(vida|sinal|contato|abdu[cç][aã]o|avistamento|intelig[eê]ncia|"
        r"civiliza[cç][aã]o|evid[eê]ncia|poss[ií]vel)\b.{0,55}\b(aliens?|extraterrestre[s]?)\b|"
        r"\b(aliens?|extraterrestre[s]?)\b.{0,55}\b(vida|sinal|contato|abdu[cç][aã]o|"
        r"avistamento|intelig[eê]ncia|civiliza[cç][aã]o|evid[eê]ncia|poss[ií]vel)\b",
        headline,
        re.IGNORECASE,
    )
    if generic_alien and not contextual_extraterrestrial:
        headline_without_generic = re.sub(r"\b(aliens?|extraterrestre[s]?)\b", "", headline, flags=re.IGNORECASE)
        if not CORE_TOPIC_RE.search(headline_without_generic):
            return False
    return bool(CORE_TOPIC_RE.search(headline) or CORE_TOPIC_RE.search(summary))


def candidate_priority(item):
    """Rank eligible feed items by direct relevance and source specialization."""
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    description = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()
    combined = f"{title} {description}"
    score = 0
    # A direct signal in the headline is stronger than a passing mention in the summary.
    if CORE_TOPIC_RE.search(title):
        score += 5
    if CORE_TOPIC_RE.search(description):
        score += 2
    source = " ".join(str(item.get(key) or "") for key in ("source_name", "source_id", "url")).lower()
    specialist_sources = (
        "mufon", "cufos", "revista_ufo", "ufo.com.br", "fenomenum",
        "cryptomundo", "unexplained-mysteries", "mysterious-universe",
        "dailygrail", "inexplicata", "vigilant_citizen", "conspiracywatch",
        "atlasobscura", "folklorethursday",
    )
    if any(marker in source for marker in specialist_sources):
        score += 3
    # Briefings/roundups rarely provide enough depth for a standalone original article.
    if re.search(r"\b(news briefs?|roundup|weekly roundup|daily briefing|not[ií]cias da semana|resumo de not[ií]cias)\b", title, re.IGNORECASE):
        score -= 5
    # General astronomy/science feeds remain eligible only for directly relevant stories,
    # but should not crowd out specialist reports when the relevance is otherwise equal.
    if any(marker in source for marker in ("nasa", "space.com", "phys.org", "sciencealert", "earthsky", "guardian")):
        score -= 1
    return score



def publisher_image_lookup(url, title):
    """Get the image attached to the original article when its feed has no image."""
    if not url:
        return None
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if "html" not in response.headers.get("Content-Type", "").lower():
                return None
            page = response.read(900000).decode("utf-8", "replace")
    except Exception:
        return None
    patterns = [
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]
    candidate = None
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            candidate = html.unescape(match.group(1).strip())
            break
    if not candidate:
        return None
    candidate = urllib.parse.urljoin(url, candidate)
    parts = urllib.parse.urlsplit(candidate)
    if parts.scheme != "https" or not parts.netloc:
        return None
    if any(word in candidate.lower() for word in ("logo", "avatar", "favicon", "sprite", "icon-")):
        return None
    return {"image_url": candidate, "image_alt": title[:250], "image_source_url": url}


def openverse_image_lookup(title):
    """Search a public image index for a title-relevant illustrative image."""
    words = re.findall(r"[a-z0-9]{3,}", str(title or "").lower())
    stop = {"the","and","for","with","from","that","this","about","after","what","when","where","who","new","says","said","uma","umas","uns","que","para","por","com","sem","sobre","entre","como","quando","onde","mais","menos","novo","nova","diz","disse","segundo","pode","podem","ser","foi","sao","dos","das"}
    query_words = [w for w in words if w not in stop]
    if not query_words:
        return None
    query = " ".join(query_words[:8])
    params = urllib.parse.urlencode({"q": query, "page_size": "20", "license_type": "commercial,modification", "aspect_ratio": "wide", "size": "medium"})
    request = urllib.request.Request("https://api.openverse.org/v1/images/?" + params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    title_terms = set(query_words)
    best = None
    best_score = 0
    for item in data.get("results", []):
        image_url = item.get("thumbnail") or item.get("url")
        if not image_url or not image_url.startswith("https://"):
            continue
        desc = " ".join(str(item.get(k) or "") for k in ("title", "tags", "creator", "source")).lower()
        desc_terms = set(re.findall(r"[a-z0-9]{3,}", desc))
        overlap = title_terms & desc_terms
        score = len(overlap) + sum(1 for word in overlap if len(word) >= 7)
        if len(overlap) < 2 or score <= best_score:
            continue
        best_score = score
        best = {"image_url": image_url, "image_alt": str(item.get("title") or title)[:250], "image_source_url": item.get("foreign_landing_url") or item.get("detail_url") or ""}
    return best

def main():
    # Enigma Mundi uses Gemini exclusively; never select another provider from secrets.
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    provider = "gemini" if api_key else ""
    model = GEMINI_MODEL if api_key else ""
    if provider == "gemini":
        try:
            model = resolve_gemini_model(api_key, model)
        except Exception as exc:
            status = {"updated_at": now_iso(), "provider": "gemini", "model": None,
                      "existing_articles": 0, "generated_this_run": 0, "status": "errors",
                      "errors": [{"error": str(exc)[:500]}]}
            write_json(STATUS_PATH, status)
            print(f"[ERROR] {str(exc)[:500]}", flush=True)
            return 0
    existing_payload = read_json(OUTPUT_PATH, {"version": 1, "articles": []})
    existing_articles = existing_payload.get("articles", []) if isinstance(existing_payload, dict) else []
    if not isinstance(existing_articles, list):
        existing_articles = []
    # Retire legacy posts that were published before the strict topic gate existed.
    relevant_articles = [a for a in existing_articles if isinstance(a, dict) and is_core_topic(a.get("title"), a.get("excerpt"))]
    removed_legacy = len(existing_articles) - len(relevant_articles)
    if removed_legacy:
        print(f"[INFO] Removendo {removed_legacy} artigos antigos fora do escopo editorial.", flush=True)
    existing_articles = relevant_articles
    # Backfill images on already-published pages, without regenerating article text.
    missing_images = [a for a in existing_articles if not a.get("image_url") and a.get("source_url")]
    if missing_images:
        print(f"[INFO] Recuperando imagens para artigos existentes: {len(missing_images)}.", flush=True)
        fixed_images = 0
        for item in missing_images:
            title = str(item.get("title") or "")
            found = publisher_image_lookup(item.get("source_url"), title)
            if not found:
                found = openverse_image_lookup(title)
            if not found or not found.get("image_url"):
                continue
            item.update(found)
            page_path = ROOT / str(item.get("path") or "")
            if page_path.is_file():
                page = page_path.read_text(encoding="utf-8")
                if '<figure class="article-cover">' not in page:
                    image = esc(found["image_url"])
                    alt = esc(found.get("image_alt") or title)
                    source = esc(item.get("source_url") or "")
                    figure = f'<figure class="article-cover"><img src="{image}" alt="{alt}" loading="eager" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="article-image-fallback" hidden>ENIGMA MUNDI · ARQUIVO VISUAL</div><figcaption>Imagem ilustrativa. <a href="{source}" rel="noopener noreferrer">Fonte da matéria</a></figcaption></figure>'
                    if '<article class="article-body">' in page:
                        page = page.replace('<article class="article-body">', figure + '\\n<article class="article-body">', 1)
                    elif '<div class="article-meta">' in page:
                        page = re.sub(r'(<div class="article-meta">.*?</div>)', r'\\1' + figure, page, count=1, flags=re.S)
                    else:
                        page = page.replace('</main>', figure + '\\n</main>', 1)
                    page_path.write_text(page, encoding="utf-8")
                fixed_images += 1
        if fixed_images:
            print(f"[INFO] Imagens adicionadas a artigos antigos: {fixed_images}.", flush=True)
    existing_urls = {canonical_url(a.get("source_url")) for a in existing_articles if a.get("source_url")}
    existing_fingerprints = {
        hashlib.sha256(canonical_url(a.get("source_url")).encode("utf-8")).hexdigest()[:10]
        for a in existing_articles if a.get("source_url")
    }
    # Avoid spending every hourly run on the exact same URLs that failed in the
    # immediately preceding run. They remain eligible again after one run, so
    # temporary publisher blocks and model outages are retried rather than lost.
    previous_status = read_json(STATUS_PATH, {})
    previous_failed_urls = {
        canonical_url(error.get("url"))
        for error in previous_status.get("errors", [])
        if isinstance(error, dict) and error.get("url")
    } if isinstance(previous_status, dict) else set()
    status = {
        "updated_at": now_iso(), "provider": provider or None, "model": model or None,
        "existing_articles": len(existing_articles), "generated_this_run": 0,
        "status": "waiting_for_api_key", "errors": [],
    }
    if not api_key:
        status["message"] = "Configure GEMINI_API_KEY em Settings > Secrets and variables > Actions para ativar a geração editorial."
        write_json(STATUS_PATH, status)
        print("GEMINI_API_KEY não configurada. A fila foi preservada; nenhum conteúdo foi inventado ou publicado.")
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
        # Reject irrelevant feed items before spending API calls or publishing them.
        if not is_core_topic(title, description):
            continue
        candidates.append(item)
    skipped_recent_failures = 0
    eligible_candidates = []
    for item in candidates:
        if canonical_url(item.get("url")) in previous_failed_urls:
            skipped_recent_failures += 1
            continue
        eligible_candidates.append(item)
    candidates = eligible_candidates
    if skipped_recent_failures:
        print(f"[INFO] Adiando {skipped_recent_failures} URLs que falharam na execução anterior; serão reavaliadas depois de uma execução.", flush=True)
    candidates.sort(
        key=lambda item: (
            candidate_priority(item),
            item.get("published_at") or "",
            item.get("discovered_at") or "",
        ),
        reverse=True,
    )
    status["status"] = "running"
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0
    attempted = 0
    source_attempts = {}
    quota_exhausted = False
    fallback_quota_exhausted = False
    rate_limited = False
    # Try up to three valid articles and inspect up to three candidates per target article.
    # Skip blocked publishers quickly; stop immediately when Gemini quota is exhausted.
    max_per_run = MAX_PER_RUN
    # Keep source failures isolated: one candidate per publisher per run.
    # This prevents a blocked feed from consuming all attempts while healthy
    # publishers have eligible stories waiting in the queue.
    for item in candidates:
        if generated >= max_per_run or attempted >= max_per_run * 3:
            break
        source_key = str(item.get("source_id") or item.get("source_name") or urllib.parse.urlsplit(str(item.get("url") or "")).netloc).strip().lower()
        if source_attempts.get(source_key, 0) >= 1:
            continue
        source_attempts[source_key] = source_attempts.get(source_key, 0) + 1
        attempted += 1
        url = canonical_url(item.get("url"))
        try:
            final_url, source_text = fetch_article(url)
            description = re.sub(r"\\s+", " ", str(item.get("description", "") or "")).strip()
            if len(source_text) < 900:
                # Mirror Tech Check's resilient behavior: a failed article-page fetch
                # must not automatically discard a candidate when its RSS entry has
                # a meaningful description. Clearly label the limited evidence for Gemini.
                if len(description) >= 250:
                    source_text = (
                        "[TEXTO INTEGRAL DA PÁGINA NÃO DISPONÍVEL; USAR SOMENTE A DESCRIÇÃO RSS ABAIXO. "
                        "Não inferir fatos ausentes nem apresentar detalhes não confirmados.]\n\n"
                        + description[:5000]
                    )
                    print(f"[INFO] Usando descrição RSS como fonte limitada ({len(description)} caracteres): {url}", flush=True)
                else:
                    raise ValueError(
                        f"Texto da página insuficiente ({len(source_text)} caracteres) e descrição RSS curta "
                        f"({len(description)} caracteres); candidato preservado para próxima execução"
                    )
            # Recheck the extracted article body. A relevant-looking headline must not
            # bypass the gate if the fetched page is unrelated or a feed is misconfigured.
            if not is_core_topic(item.get("title", ""), item.get("description", ""), source_text[:10000]):
                raise ValueError("Conteúdo fora do escopo editorial do Enigma Mundi; artigo descartado pelo filtro temático")
            category = item.get("category") or "misterios"
            source_name = item.get("source_name") or item.get("source_id") or "Fonte externa"
            article, used_model = call_model_with_fallback(api_key, provider, model, item["title"], item.get("description", ""), source_name, final_url, source_text, category)
            status["model"] = used_model
            status["provider"] = "gemini" if used_model.startswith("gemini-") else provider
            if not is_core_topic(article["title"], article["excerpt"]):
                raise ValueError("Título/resumo gerado não comprova aderência ao escopo do Enigma Mundi; artigo descartado")
            fingerprint = hashlib.sha256(canonical_url(final_url).encode("utf-8")).hexdigest()[:10]
            if canonical_url(final_url) in existing_urls or fingerprint in existing_fingerprints:
                print(f"[SKIP] Fonte já publicada; duplicata bloqueada: {final_url}", flush=True)
                continue
            slug = slugify(article["title"])[:60] + "-" + fingerprint
            output_path = ARTICLES_DIR / f"{slug}.html"
            if output_path.exists():
                existing_urls.add(url)
                continue
            published_at = item.get("published_at") or now_iso()
            image_data = {"image_url": item.get("image_url"), "image_alt": article["title"] if item.get("image_url") else None, "image_source_url": final_url if item.get("image_url") else None}
            if not image_data["image_url"]:
                image_data = publisher_image_lookup(final_url, article["title"]) or image_data
            if not image_data.get("image_url"):
                image_data = openverse_image_lookup(article["title"]) or image_data
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
                "image_url": image_data.get("image_url"),
                "image_source_url": image_data.get("image_source_url"),
                "image_alt": image_data.get("image_alt"),
                "tags": article["tags"],
            }
            output_path.write_text(render_article_page(article, record, slug), encoding="utf-8")
            existing_articles.append(record)
            existing_urls.add(canonical_url(final_url))
            existing_fingerprints.add(fingerprint)
            generated += 1
            print(f"[OK] Artigo criado: {record['path']} — {record['title']}", flush=True)
            time.sleep(1)
        except Exception as exc:
            raw_error = str(exc)
            message = f"{item.get('source_name', item.get('source_id', 'fonte'))}: {raw_error[:400]}"
            status["errors"].append({"url": url, "error": message})
            print(f"[WARN] {message}", flush=True)
            if "QUOTA_EXHAUSTED:" in raw_error:
                gemini_fallback_quota = provider == "groq" and "fallback Gemini" in raw_error
                groq_quota_also_exhausted = "Groq: API groq retornou HTTP 429" in raw_error
                if gemini_fallback_quota and not groq_quota_also_exhausted:
                    fallback_quota_exhausted = True
                    print("[WARN] A cota gratuita do Gemini fallback foi atingida; continuando as tentativas Groq com os demais candidatos.", flush=True)
                else:
                    quota_exhausted = True
                    print(f"[ERROR] Cota do provedor principal {provider} esgotada. Interrompendo novas chamadas de IA; os critérios editoriais permanecem inalterados.", flush=True)
                    break
            if "RATE_LIMITED:" in raw_error:
                rate_limited = True
                print(f"[WARN] Limite temporário de tokens/minuto da API {provider}. Interrompendo esta execução; novas tentativas ocorrerão no próximo horário agendado.", flush=True)
                break
    existing_articles.sort(key=lambda a: (a.get("published_at", ""), a.get("generated_at", "")), reverse=True)
    write_json(OUTPUT_PATH, {
        "version": 1, "generated_at": now_iso(),
        "article_count": len(existing_articles), "articles": existing_articles,
    })
    status.update({
        "updated_at": now_iso(),
        "status": ("partial_quota_exhausted" if quota_exhausted and generated else
                   "quota_exhausted" if quota_exhausted else
                   "partial_fallback_quota_exhausted" if fallback_quota_exhausted and generated else
                   "fallback_quota_exhausted" if fallback_quota_exhausted else
                   "partial_rate_limited" if rate_limited and generated else
                   "rate_limited" if rate_limited else
                   "completed" if generated else ("errors" if status["errors"] else "no_new_candidates")),
        "existing_articles": len(existing_articles),
        "generated_this_run": generated,
        "candidate_count": len(candidates),
        "attempted_this_run": attempted,
        "message": (f"Cota do provedor principal {provider} atingida; geração interrompida para evitar tentativas inúteis. Reportagens válidas já geradas foram preservadas." if quota_exhausted else
                    "Cota gratuita do Gemini fallback atingida; a execução tentou continuar com o Groq. Reportagens válidas já geradas foram preservadas." if fallback_quota_exhausted else
                    f"Limite temporário de tokens/minuto do {provider}; a execução foi pausada e será retomada no próximo horário agendado. Reportagens válidas já geradas foram preservadas." if rate_limited else
                    f"Geração automática ativa: até {max_per_run} artigo(s) por execução; no máximo uma tentativa por fonte em cada execução para diversificar os candidatos quando há bloqueios.")
    })
    write_json(STATUS_PATH, status)
    print(f"Artigos novos: {generated}; total publicado: {len(existing_articles)}; erros: {len(status['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
