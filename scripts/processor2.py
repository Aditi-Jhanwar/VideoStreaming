#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
processor2.py (single-file pipeline)

Flow:
1) Fetch/repair published_dt using REGEX (including visible page TEXT regex).
2) Drop items older than DAYS.
3) Drop items whose date is still missing/unrecoverable.
4) LLM filter (Groq): news vs non-news + relevance
   - IMPORTANT: If LLM fails for any batch OR item missing in response -> KEEP that item.
5) Remove duplicates WITHOUT LLM using title similarity >= 0.50.
6) Summarize final kept articles into EXACTLY 4 lines (Groq), saved as summary_4l.
7) Write final_news.json (+ rejected2.json + duplicates.json) into NXP_autonews/data/
"""

import os
import json
import re
import time
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

import requests
from dateutil import parser as dtparser
from dateutil import tz

# optional full-text extraction
try:
    import trafilatura
except Exception:
    trafilatura = None


# ============================================================
# PATHS
# ============================================================
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))   # .../scripts
BASE_DIR = os.path.dirname(SCRIPTS_DIR)                    # .../NXP_autonews
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

IN_FILE = os.path.join(DATA_DIR, "processed_news.json")
OUT_FINAL = os.path.join(DATA_DIR, "final_news.json")
OUT_DUPES = os.path.join(DATA_DIR, "duplicates.json")
OUT_REJECTED2 = os.path.join(DATA_DIR, "rejected2.json")
SUMMARY_CACHE_FILE = os.path.join(DATA_DIR, "summary_cache.json")


# ============================================================
# CONFIG
# ============================================================
LOCAL_TZ = tz.gettz("Asia/Kolkata")
DAYS = 7

# Date fetch
FETCH_DATE_IF_MISSING = True
MAX_DATE_FETCH = 999999
MAX_WORKERS = 10
HTTP_TIMEOUT = 18
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
HTML_MAX_CHARS = 900000
REJECT_IF_DATE_UNRECOVERABLE = True

# Duplicate removal
DUP_SIM_THRESHOLD = 0.50

# Groq LLM
USE_LLM_NEWS_FILTER = True
GROQ_API_KEY = ""  # hardcoded as you asked
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

# Make batches visible + stable
LLM_BATCH_SIZE = 1
LLM_MAX_BATCH_CHARS = 14000
MAX_LLM_OUTPUT_TOKENS = 900
SLEEP_BETWEEN_LLM_BATCHES = 2.0
LLM_MAX_RETRIES = 3

# Summarization
ENABLE_SUMMARY = True
SUMMARY_LINES = 4
SUMMARY_MAX_TOKENS = 240
SUMMARY_RETRIES = 3
SLEEP_BETWEEN_SUMMARIES = 0.5
SUMMARY_MAX_CHARS = 14000

FETCH_FULLTEXT_FOR_SUMMARY = True
FULLTEXT_MAX_CHARS = 20000

# Prefer stronger sources when duplicates exist
PREFERRED_DOMAINS = [
    "economictimes.indiatimes.com",
    "m.economictimes.com",
    "telecom.economictimes.indiatimes.com",
    "manufacturing.economictimes.indiatimes.com",
    "thehindu.com",
    "indianexpress.com",
    "business-standard.com",
    "pib.gov.in",
    "moneycontrol.com",
    "businesstoday.in",
    "reuters.com",
    "bloomberg.com",
]

# Remove tracking params
TRACKING_PARAMS = {"gclid", "fbclid", "igshid", "mc_cid", "mc_eid"}
TRACKING_PREFIXES = ("utm_",)


# ============================================================
# IO
# ============================================================
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_cache(path: str) -> Dict[str, Any]:
    x = load_json(path, default={})
    return x if isinstance(x, dict) else {}

def save_cache(path: str, cache: Dict[str, Any]) -> None:
    save_json(path, cache)


# ============================================================
# URL Helpers
# ============================================================
def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        u = urlparse(url)
        q = []
        for k, v in parse_qsl(u.query, keep_blank_values=True):
            kl = (k or "").lower()
            if kl in TRACKING_PARAMS:
                continue
            if any(kl.startswith(p) for p in TRACKING_PREFIXES):
                continue
            q.append((k, v))
        return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))
    except Exception:
        return url.strip()

def domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:
        return ""

def preferred_score(domain: str) -> int:
    d = (domain or "").lower()
    for i, p in enumerate(PREFERRED_DOMAINS):
        if p in d:
            return 100 - i
    return 0


# ============================================================
# Datetime Helpers
# ============================================================
def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = dtparser.parse(s, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt = dt.astimezone(LOCAL_TZ)
        return dt
    except Exception:
        return None

def is_within_days(dt: datetime, days: int) -> bool:
    now = datetime.now(LOCAL_TZ)
    return dt >= (now - timedelta(days=days))


# ============================================================
# Fetch HTML + Visible text
# ============================================================
def fetch_html(url: str) -> str:
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
        if not r.ok:
            return ""
        return (r.text or "")[:HTML_MAX_CHARS]
    except Exception:
        return ""

def html_to_visible_text(html: str) -> str:
    if not html:
        return ""
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ============================================================
# DATE EXTRACTION (REGEX-FIRST)
# ============================================================
ISO_DATE_ANYWHERE = r"\b(20\d{2}[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])(?:[T\s][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:\.\d+)?(?:Z|[+-][0-2]\d:?[0-5]\d)?)?)\b"
EPOCH_MS = r"\b(1[6-9]\d{11}|2\d{12})\b"
EPOCH_S  = r"\b(1[6-9]\d{9}|2\d{10})\b"

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
HUMAN_DATE_REGEXES = [
    rf"\b(?:updated|published|posted|last updated|modified)\s*[:\-]?\s*({MONTH}\s+\d{{1,2}},\s+20\d{{2}}(?:\s+\d{{1,2}}:\d{{2}}(?:\s*(?:am|pm))?)?(?:\s*(?:ist|gmt|utc))?)\b",
    rf"\b({MONTH}\s+\d{{1,2}},\s+20\d{{2}}(?:\s+\d{{1,2}}:\d{{2}}(?:\s*(?:am|pm))?)?(?:\s*(?:ist|gmt|utc))?)\b",
    rf"\b(\d{{1,2}}\s+{MONTH}\s+20\d{{2}}(?:\s+\d{{1,2}}:\d{{2}}(?:\s*(?:am|pm))?)?(?:\s*(?:ist|gmt|utc))?)\b",
    r"\b(20\d{2}[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])(?:\s+[0-2]\d:[0-5]\d(?::[0-5]\d)?)?)\b",
]

META_DATE_PATTERNS = [
    r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']',
    r'name=["\']publish-date["\']\s+content=["\']([^"\']+)["\']',
    r'itemprop=["\']datePublished["\']\s+content=["\']([^"\']+)["\']',
]
JSON_DATE_KEYS = ["datePublished", "publishedAt", "pubDate", "publishDate", "published_time", "publishedTime"]

def extract_date_candidates_regex(html: str, visible_text: str) -> List[str]:
    cands: List[str] = []
    for pat in META_DATE_PATTERNS:
        m = re.search(pat, html or "", flags=re.I)
        if m:
            cands.append(m.group(1).strip())

    m = re.search(r'(?is)<time[^>]+datetime=["\']([^"\']+)["\']', html or "")
    if m:
        cands.append(m.group(1).strip())

    for k in JSON_DATE_KEYS:
        cands.extend(re.findall(rf'"{re.escape(k)}"\s*:\s*"([^"]+)"', html or "", flags=re.I))
        cands.extend(re.findall(rf'{re.escape(k)}\s*:\s*"([^"]+)"', html or "", flags=re.I))
        cands.extend(re.findall(rf'"{re.escape(k)}"\s*:\s*([0-9]{{10,13}})', html or "", flags=re.I))

    cands.extend([x[0] if isinstance(x, tuple) else x for x in re.findall(ISO_DATE_ANYWHERE, html or "")])
    cands.extend(re.findall(EPOCH_MS, html or "")[:40])
    cands.extend(re.findall(EPOCH_S, html or "")[:40])

    txt = visible_text or ""
    for pat in HUMAN_DATE_REGEXES:
        cands.extend(re.findall(pat, txt, flags=re.I))
    cands.extend([x[0] if isinstance(x, tuple) else x for x in re.findall(ISO_DATE_ANYWHERE, txt)])

    out, seen = [], set()
    for x in cands:
        sx = str(x).strip()
        if sx and sx not in seen:
            seen.add(sx)
            out.append(sx)
    return out

def best_publish_datetime(candidates: List[str]) -> Optional[datetime]:
    parsed: List[datetime] = []
    for c in candidates:
        try:
            cs = str(c).strip()
            if re.fullmatch(r"\d{10,13}", cs):
                vv = int(cs)
                if vv > 10**12:
                    dt = datetime.fromtimestamp(vv / 1000.0, tz=tz.UTC).astimezone(LOCAL_TZ)
                else:
                    dt = datetime.fromtimestamp(vv, tz=tz.UTC).astimezone(LOCAL_TZ)
                parsed.append(dt)
                continue
            dt = dtparser.parse(cs, fuzzy=True)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            else:
                dt = dt.astimezone(LOCAL_TZ)
            parsed.append(dt)
        except Exception:
            continue
    if not parsed:
        return None
    parsed.sort()
    return parsed[0]

def extract_publish_date_regex_only(html: str) -> Optional[str]:
    if not html:
        return None
    text = html_to_visible_text(html)
    candidates = extract_date_candidates_regex(html, text)
    dt = best_publish_datetime(candidates)
    return dt.isoformat() if dt else None


# ============================================================
# Groq helper + ping (PROOF it is called)
# ============================================================
def groq_chat(messages: List[Dict[str, str]], max_tokens: int, temperature: float = 0.0) -> str:
    headers = {
        "Authorization": "Bearer %s" % GROQ_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
    if not r.ok:
        raise RuntimeError("Groq error %s: %s" % (r.status_code, (r.text or "")[:800]))
    data = r.json() or {}
    return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()

def groq_ping() -> bool:
    try:
        t0 = time.time()
        out = groq_chat([{"role": "user", "content": "Return JSON only: {\"ok\":true}"}], max_tokens=30, temperature=0.0)
        dt = time.time() - t0
        print(f"?? Groq ping ok in {dt:.2f}s, reply: {out[:80]}", flush=True)
        return True
    except Exception as e:
        print("? Groq ping failed:", str(e), flush=True)
        return False


# ============================================================
# LLM filter
# ============================================================
COMPANY_CONTEXT = """
You are filtering news for a semiconductor/electronics company operating in India.

Keep only genuine NEWS articles relevant to ANY of these:
A) Semiconductor/Electronics:
- Semiconductors / chips / fabs / foundries / OSAT / ATMP / packaging
- India Semiconductor Mission / Semicon India / ISM incentives/policies
- MeitY announcements, electronics & IT ministry policies
- Ashwini Vaishnaw, S Krishnan (policy/industry announcements)
- India AI Mission (government or major industry moves)

B) India Trade/Policy/Economy:
- Trade: tariffs, duties, exports/imports, FTAs, WTO disputes, trade pacts
- Policy: cabinet decisions, bills/acts, regulations, schemes, govt notifications
- Economy: RBI policy, repo rate, inflation (CPI/WPI), GDP, budget, PMI/IIP,
  rupee/forex reserves, FDI

Drop:
- explainers/definitions/tutorials
- social posts, Wikipedia/LinkedIn
- job posts, courses
""".strip()

def build_llm_batches(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    batches: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0

    for it in items:
        blob = json.dumps(
            {
                "id": it.get("_id"),
                "title": it.get("title"),
                "domain": it.get("domain"),
                "url": it.get("url"),
                "published_dt": it.get("published_dt"),
                "content": (it.get("content") or "")[:550],
            },
            ensure_ascii=False,
        )
        if cur and (len(cur) >= LLM_BATCH_SIZE or (cur_chars + len(blob)) > LLM_MAX_BATCH_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(it)
        cur_chars += len(blob)

    if cur:
        batches.append(cur)
    return batches

def llm_filter_news(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    KEEP-on-failure logic:
    - If batch fails OR an item is missing in response -> KEEP item as llm_failed_keep.
    - Reject ONLY when Groq explicitly says is_news=false OR keep=false.
    """
    if not USE_LLM_NEWS_FILTER:
        return items, []

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    batches = build_llm_batches(items)
    print(f"?? LLM: {len(items)} items -> {len(batches)} batches (batch_size={LLM_BATCH_SIZE})", flush=True)

    for bi, batch in enumerate(batches, start=1):
        print(f"?? LLM batch {bi}/{len(batches)} starting (items={len(batch)})", flush=True)
        t0 = time.time()

        payload_items = [
            {
                "id": it["_id"],
                "title": it.get("title", ""),
                "domain": it.get("domain", ""),
                "url": it.get("url", ""),
                "published_dt": it.get("published_dt", ""),
                "content": (it.get("content") or "")[:550],
            }
            for it in batch
        ]

        prompt = """
%s

For each item decide:
- is_news: true only if it's a news report (NOT explainer/tutorial/profile)
- keep: true only if relevant to the company context above
- relevance_score: integer 0-10
- reason: max 12 words

Return STRICT JSON only:
{
  "items":[
    {"id":"...","is_news":true/false,"keep":true/false,"relevance_score":0-10,"reason":"..."}
  ]
}

Items:
%s
""".strip() % (COMPANY_CONTEXT, json.dumps(payload_items, ensure_ascii=False))

        messages = [
            {"role": "system", "content": "Output ONLY valid JSON. No extra text."},
            {"role": "user", "content": prompt},
        ]

        got: Dict[str, Any] = {}
        last_err = ""
        for attempt in range(1, LLM_MAX_RETRIES + 1):
            try:
                raw = groq_chat(messages, max_tokens=MAX_LLM_OUTPUT_TOKENS, temperature=0.0)
                raw2 = raw.strip()
                raw2 = re.sub(r"^```(json)?\s*", "", raw2, flags=re.I)
                raw2 = re.sub(r"\s*```$", "", raw2)
                data = json.loads(raw2)
                got = {x.get("id"): x for x in (data.get("items") or []) if x.get("id")}
                break
            except Exception as e:
                last_err = str(e)
                time.sleep(2.0 * attempt)

        dt = time.time() - t0

        if not got:
            print(f"?? LLM batch {bi} FAILED in {dt:.2f}s -> KEEP ALL. err={last_err[:160]}", flush=True)
            for it in batch:
                it["llm2"] = {
                    "is_news": True,
                    "keep": True,
                    "relevance_score": 0,
                    "reason": "llm_failed_keep",
                    "llm_failed": True,
                }
        else:
            print(f"? LLM batch {bi} OK in {dt:.2f}s (judgements={len(got)})", flush=True)
            for it in batch:
                rid = it["_id"]
                x = got.get(rid)
                if not x:
                    # item missing => KEEP
                    it["llm2"] = {
                        "is_news": True,
                        "keep": True,
                        "relevance_score": 0,
                        "reason": "llm_missing_keep",
                        "llm_failed": True,
                    }
                else:
                    it["llm2"] = {
                        "is_news": bool(x.get("is_news", False)),
                        "keep": bool(x.get("keep", False)),
                        "relevance_score": int(x.get("relevance_score", 0) or 0),
                        "reason": (x.get("reason") or "").strip(),
                        "llm_failed": False,
                    }

        time.sleep(SLEEP_BETWEEN_LLM_BATCHES)

    for it in items:
        ll = it.get("llm2") or {}
        if ll.get("llm_failed"):
            accepted.append(it)
            continue
        if ll.get("is_news") and ll.get("keep"):
            accepted.append(it)
        else:
            rejected.append({**it, "_reject_reason2": "llm_non_news_or_irrelevant"})

    return accepted, rejected


# ============================================================
# Duplicate removal (NO LLM)
# ============================================================
STOPWORDS = {
    "the","a","an","and","or","of","to","in","for","on","with","as",
    "india","indian","latest","today","report","news","says","said"
}

def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"[\W_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    toks = [w for w in t.split() if w and w not in STOPWORDS]
    return " ".join(toks)

def title_similarity(a: str, b: str) -> float:
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()

def pick_better(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    a_ll = (a.get("llm2") or {})
    b_ll = (b.get("llm2") or {})
    a_score = int(a_ll.get("relevance_score", 0) or 0)
    b_score = int(b_ll.get("relevance_score", 0) or 0)
    if a_score != b_score:
        return a if a_score > b_score else b

    a_dom = preferred_score(a.get("domain", ""))
    b_dom = preferred_score(b.get("domain", ""))
    if a_dom != b_dom:
        return a if a_dom > b_dom else b

    a_dt = parse_dt(a.get("published_dt"))
    b_dt = parse_dt(b.get("published_dt"))
    a_ts = int(a_dt.timestamp()) if a_dt else 0
    b_ts = int(b_dt.timestamp()) if b_dt else 0
    if a_ts != b_ts:
        return a if a_ts > b_ts else b

    a_len = len(a.get("content") or "")
    b_len = len(b.get("content") or "")
    return a if a_len >= b_len else b

def dedupe_by_similarity(items: List[Dict[str, Any]], threshold: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept: List[Dict[str, Any]] = []
    dupes: List[Dict[str, Any]] = []

    for it in items:
        is_dupe = False
        for j, kept_it in enumerate(kept):
            sim = title_similarity(it.get("title", ""), kept_it.get("title", ""))
            if sim >= threshold:
                winner = pick_better(kept_it, it)
                loser = it if winner is kept_it else kept_it
                if winner is it:
                    kept[j] = it
                dupes.append({**loser, "_dupe_of": winner.get("url"), "_dupe_sim": round(sim, 3)})
                is_dupe = True
                break
        if not is_dupe:
            kept.append(it)

    return kept, dupes


# ============================================================
# Summarization
# ============================================================
def extract_fulltext_for_summary(url: str) -> str:
    if not url:
        return ""
    if trafilatura is not None:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                txt = trafilatura.extract(downloaded, include_tables=True, include_comments=False) or ""
                txt = re.sub(r"\s+", " ", txt).strip()
                return txt[:FULLTEXT_MAX_CHARS]
        except Exception:
            pass
    html = fetch_html(url)
    return html_to_visible_text(html)[:FULLTEXT_MAX_CHARS]

def enforce_exact_lines(text: str, n: int) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < n and len(lines) == 1:
        parts = [p.strip() for p in re.split(r"(?<=\.)\s+", lines[0]) if p.strip()]
        lines = parts
    return "\n".join(lines[:n])

def summarize_item_4l(it: Dict[str, Any], cache: Dict[str, Any]) -> str:
    url = (it.get("url") or "").strip()
    if not url:
        return ""

    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    if key in cache and isinstance(cache[key], str) and cache[key].strip():
        return cache[key].strip()

    text = ""
    if FETCH_FULLTEXT_FOR_SUMMARY:
        text = extract_fulltext_for_summary(url)
    if not text:
        text = (it.get("content") or "")[:4000]

    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    text = text[:SUMMARY_MAX_CHARS]

    prompt = (
        "Summarize the following news article in EXACTLY 4 lines.\n"
        "Rules:\n"
        "1) Exactly 4 lines (each line a complete sentence).\n"
        "2) Cover ALL important points and key numbers.\n"
        "3) No bullets, no heading, no extra lines.\n\n"
        "ARTICLE:\n" + text
    )

    messages = [
        {"role": "system", "content": "You are a precise news summarizer."},
        {"role": "user", "content": prompt},
    ]

    last_err = ""
    for attempt in range(1, SUMMARY_RETRIES + 1):
        try:
            raw = groq_chat(messages, max_tokens=SUMMARY_MAX_TOKENS, temperature=0.2)
            out = enforce_exact_lines(raw, SUMMARY_LINES).strip()
            if out:
                cache[key] = out
                return out
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * attempt)

    it["_summary_error"] = last_err[:250]
    return ""


# ============================================================
# ID helper
# ============================================================
def make_id(url: str, title: str) -> str:
    h = hashlib.sha256()
    h.update((url or "").encode("utf-8"))
    h.update(b"|")
    h.update((title or "").encode("utf-8"))
    return h.hexdigest()


# ============================================================
# MAIN
# ============================================================
def main():
    print("? RUNNING:", os.path.abspath(__file__), flush=True)

    items = load_json(IN_FILE, default=[])
    if not isinstance(items, list):
        raise RuntimeError("%s must be a JSON list" % IN_FILE)

    rejected2: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = []

    # Normalize + id
    for it in items:
        if not isinstance(it, dict):
            continue
        url = canonicalize_url((it.get("url") or "").strip())
        title = (it.get("title") or "").strip()
        domain = (it.get("domain") or domain_from_url(url)).lower()
        content = it.get("content") or it.get("snippet") or ""

        if not url or not title:
            rejected2.append({**it, "_reject_reason2": "missing_url_or_title"})
            continue

        it2 = dict(it)
        it2["url"] = url
        it2["title"] = title
        it2["domain"] = domain
        it2["content"] = content
        it2["_id"] = make_id(url, title)
        pool.append(it2)

    # 1) Date fill
    if FETCH_DATE_IF_MISSING:
        needs_date = [x for x in pool if not parse_dt(x.get("published_dt"))]
        needs_date = needs_date[:MAX_DATE_FETCH]
        if needs_date:
            print("?? Regex date extraction for missing dates:", len(needs_date), flush=True)

            def worker(x: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
                url = x.get("url") or ""
                if not url:
                    return ("", None, "no_url")
                html = fetch_html(url)
                if not html:
                    return (url, None, "html_fetch_failed")
                iso = extract_publish_date_regex_only(html)
                if not iso:
                    return (url, None, "regex_date_not_found")
                dt = parse_dt(iso)
                if not dt:
                    return (url, None, "regex_date_parse_failed")
                return (url, dt.isoformat(), "ok")

            url_to_date: Dict[str, Tuple[Optional[str], str]] = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = [ex.submit(worker, x) for x in needs_date]
                for fut in as_completed(futs):
                    url, iso, status = fut.result()
                    url_to_date[url] = (iso, status)

            for x in needs_date:
                u = x.get("url") or ""
                iso, status = url_to_date.get(u, (None, "missing_result"))
                x["_date_fill_status"] = status
                if iso:
                    x["published_dt"] = iso

    # 2) Drop old + drop missing date
    pool2: List[Dict[str, Any]] = []
    for it in pool:
        dt = parse_dt(it.get("published_dt"))
        if not dt:
            if REJECT_IF_DATE_UNRECOVERABLE:
                rejected2.append({**it, "_reject_reason2": "date_unrecoverable:%s" % it.get("_date_fill_status", "unknown")})
                continue
            pool2.append(it)
            continue
        if not is_within_days(dt, DAYS):
            rejected2.append({**it, "_reject_reason2": "older_than_%d_days" % DAYS})
            continue
        pool2.append(it)

    print("?? After date filter:", len(pool2), "items", flush=True)

    # 3) LLM filter
    llm_rejected: List[Dict[str, Any]] = []
    if USE_LLM_NEWS_FILTER:
        groq_ping()  # proof
        if len(pool2) == 0:
            pool3 = []
        else:
            pool3, llm_rejected = llm_filter_news(pool2)
            rejected2.extend(llm_rejected)
    else:
        pool3 = pool2

    # 4) Dedupe
    unique, dupes = dedupe_by_similarity(pool3, DUP_SIM_THRESHOLD)

    # 5) Sort newest first
    def sort_key(x: Dict[str, Any]) -> int:
        dt = parse_dt(x.get("published_dt"))
        return int(dt.timestamp()) if dt else 0

    unique.sort(key=sort_key, reverse=True)

    # 6) Summary 4 lines (final articles only)
    if ENABLE_SUMMARY and unique:
        print(f"?? Summarizing {len(unique)} final articles (4 lines each)...", flush=True)
        cache = load_cache(SUMMARY_CACHE_FILE)

        for idx, it in enumerate(unique, start=1):
            if it.get("summary_4l"):
                continue
            it["summary_4l"] = summarize_item_4l(it, cache)
            if idx % 10 == 0:
                save_cache(SUMMARY_CACHE_FILE, cache)
                print("  ...summarized", idx, "/", len(unique), flush=True)
            time.sleep(SLEEP_BETWEEN_SUMMARIES)

        save_cache(SUMMARY_CACHE_FILE, cache)

    # outputs
    save_json(OUT_FINAL, unique)
    save_json(OUT_DUPES, dupes)
    save_json(OUT_REJECTED2, rejected2)

    print("? processor2 done", flush=True)
    print("  Input processed_news:", len(items), flush=True)
    print("  After strict date+7days:", len(pool2), flush=True)
    print("  After LLM non-news filter:", len(pool3), flush=True)
    print("  Final unique:", len(unique), "->", OUT_FINAL, flush=True)
    print("  Duplicates removed:", len(dupes), "->", OUT_DUPES, flush=True)
    print("  Rejected2:", len(rejected2), "->", OUT_REJECTED2, flush=True)

if __name__ == "__main__":
    main()
