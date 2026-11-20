"""Drafts German replies to eBay buyer messages with a free-tier LLM.

Providers are tried in order until one answers, so a rate-limited free
tier just falls through to the next. Personal data (emails, phone numbers,
street addresses, the buyer's name) is masked BEFORE anything leaves the
server; the model only ever sees placeholders.
"""

import re

import httpx

from app.config import settings

SYSTEM_PROMPT = (
    "Du bist der freundliche Kundenservice eines kleinen eBay-Shops in Deutschland. "
    "Antworte kurz, höflich und auf Deutsch (Sie-Form). Nutze NUR die gegebenen Bestell-Fakten. "
    "Erfinde nichts: Wenn eine Information fehlt (z. B. Liefertermin), sage ehrlich, dass du nachsiehst. "
    "Keine Zusagen zu Erstattung oder Schadenersatz. Schreibe nur den Antworttext, mit Anrede und Gruß."
)

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[E-MAIL]"),
    (re.compile(r"(?<!\d)(?:\+|00)?\d[\d /().-]{7,}\d"), "[TELEFON]"),
    (re.compile(r"\b[A-ZÄÖÜ][\wäöüß.-]*(?:straße|strasse|str\.|weg|platz|allee|gasse)\s*\d+\s*\w?", re.I), "[ADRESSE]"),
    (re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][\wäöüß-]+"), "[PLZ ORT]"),
]


def mask_personal_data(text: str, names: list[str] | None = None) -> str:
    for name in names or []:
        for part in {name, *name.split()}:
            if len(part) >= 3:
                text = re.sub(re.escape(part), "[KUNDE]", text, flags=re.I)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _providers() -> list[tuple[str, callable]]:
    chain = []
    if settings.groq_api_key:
        chain.append(("groq", lambda m: _openai_style(
            "https://api.groq.com/openai/v1/chat/completions", settings.groq_api_key, settings.groq_model, m)))
    if settings.gemini_api_key:
        chain.append(("gemini", _gemini))
    if settings.openrouter_api_key:
        chain.append(("openrouter", lambda m: _openai_style(
            "https://openrouter.ai/api/v1/chat/completions", settings.openrouter_api_key, settings.openrouter_model, m)))
    if settings.nvidia_api_key:
        chain.append(("nvidia", lambda m: _openai_style(
            "https://integrate.api.nvidia.com/v1/chat/completions", settings.nvidia_api_key, settings.nvidia_model, m)))
    return chain


def _openai_style(url: str, key: str, model: str, user_msg: str) -> str:
    resp = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=30.0, json={
        "model": model, "temperature": 0.3, "max_tokens": 400,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _gemini(user_msg: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    resp = httpx.post(url, headers={"x-goog-api-key": settings.gemini_api_key}, timeout=30.0, json={
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 400}})
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def configured() -> bool:
    return bool(_providers())


def suggest_reply(message: str, facts: str = "", buyer_names: list[str] | None = None) -> dict:
    chain = _providers()
    if not chain:
        raise ValueError("Kein KI-Schlüssel eingerichtet (GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY oder NVIDIA_API_KEY).")
    prompt = (
        f"Bestell-Fakten:\n{mask_personal_data(facts, buyer_names) or '(keine)'}\n\n"
        f"Nachricht des Käufers:\n{mask_personal_data(message, buyer_names)}\n\nSchreibe die Antwort."
    )
    errors = []
    for name, call in chain:
        try:
            reply = call(prompt)
            if reply:
                return {"reply": reply, "provider": name}
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    raise ValueError("Alle KI-Anbieter fehlgeschlagen: " + "; ".join(errors))
