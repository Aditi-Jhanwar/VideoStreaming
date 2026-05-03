#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# scripts/summarizer_api.py  (Python 3.6 safe)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import re
import requests

# ==============================
# GROQ CONFIG
# ==============================
GROQ_API_KEY = ""
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ==============================
# HTTP CONFIG
# ==============================
HTTP_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

MAX_ARTICLE_CHARS = 12000
MIN_ARTICLE_CHARS = 500
MAX_TOKENS = 260

# ==============================
# FASTAPI APP
# ==============================
app = FastAPI(title="AutoNews Groq Summarizer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class SummReq(BaseModel):
    url: str

# ==============================
# TEXT EXTRACTION (no trafilatura)
# ==============================
def _clean_html_to_text(html):
    if not html:
        return ""
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def extract_text(url):
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="Missing url")
    url = url.strip()

    try:
        r = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail="Fetch failed: %s" % e)

    if not r.ok:
        raise HTTPException(status_code=400, detail="Fetch failed HTTP %s" % r.status_code)

    txt = _clean_html_to_text(r.text or "")
    txt = (txt or "").strip()

    if len(txt) < MIN_ARTICLE_CHARS:
        raise HTTPException(status_code=400, detail="Extracted text too short (blocked/paywalled?)")

    return txt[:MAX_ARTICLE_CHARS]

# ==============================
# GROQ CALL
# ==============================
def groq_summary_4_bullets(article_text):
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key not set")

    prompt = (
        "You are a senior news editor.\n\n"
        "Summarize the article in EXACTLY 4 bullet points.\n"
        "STRICT RULES:\n"
        "1) Output exactly 4 lines.\n"
        "2) Each line MUST start with '- ' (dash and space).\n"
        "3) Each bullet must contain IMPORTANT facts (who/what/when/where/numbers).\n"
        "4) Do NOT add anything not in the article. Do NOT write filler.\n"
        "5) No heading, no intro, no conclusion, no blank lines.\n\n"
        "ARTICLE:\n"
        + article_text
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Return ONLY 4 bullets. Exactly 4 lines. Each starts with '- '."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
    }

    try:
        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": "Bearer %s" % GROQ_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=40,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail="Groq request failed: %s" % e)

    if not r.ok:
        raise HTTPException(status_code=500, detail="Groq error %s: %s" % (r.status_code, (r.text or "")[:1200]))

    data = r.json() or {}
    text = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
    return (text or "").strip()

# ==============================
# ENFORCE EXACT 4 BULLETS
# ==============================
def enforce_4_bullets(raw):
    if not raw:
        return ""

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    cleaned = []
    for ln in lines:
        ln2 = re.sub(r"^\s*[-•*]\s*", "", ln).strip()
        ln2 = re.sub(r"^\s*\d+\s*[\)\.\-]\s*", "", ln2).strip()
        if ln2:
            cleaned.append(ln2)

    if len(cleaned) == 1:
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned[0]) if p.strip()]
        if len(parts) > 1:
            cleaned = parts

    cleaned = cleaned[:4]

    # If model returned <4, repeat last factual bullet (no "not stated" filler).
    while len(cleaned) < 4 and cleaned:
        cleaned.append(cleaned[-1])

    if len(cleaned) != 4:
        return ""

    return "\n".join(["- " + x for x in cleaned])

# ==============================
# ENDPOINT
# ==============================
@app.post("/summarize")
def summarize(req: SummReq):
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")

    article_text = extract_text(url)
    raw = groq_summary_4_bullets(article_text)
    out = enforce_4_bullets(raw)

    if not out:
        raise HTTPException(status_code=500, detail="Could not produce 4 bullet summary")

    return {"summary": out}
