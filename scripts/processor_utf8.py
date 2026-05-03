#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import time
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as dtparser
from dateutil import tz

# ===============================
# PATHS
# ===============================
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPTS_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

INPUT_FILE = os.path.join(DATA_DIR, "matched_news.json")
OUTPUT_JSON = os.path.join(DATA_DIR, "processed_news.json")
OUTPUT_TXT = os.path.join(DATA_DIR, "processed_news.txt")
REJECTED_JSON = os.path.join(DATA_DIR, "rejected_news.json")
REJECTED_TXT = os.path.join(DATA_DIR, "rejected_news.txt")
LLM_CACHE_FILE = os.path.join(DATA_DIR, "llm_judgements_cache.json")

# ===============================
# LOGGING (Python 3.6 safe)
# ===============================
LOG_FILE = os.path.join(LOG_DIR, "processor.log")
logger = logging.getLogger("processor")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
fh.setFormatter(fmt)
logger.addHandler(fh)

# ===============================
# CONFIG
# ===============================
GROQ_API_KEY = ""
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

LOCAL_TZ = tz.gettz("Asia/Kolkata")
DAYS = 7

KEEP_IF_DATE_UNKNOWN = True
MIN_RELEVANCE_SCORE = 6

PREFERRED_DOMAINS = [
    "economictimes.indiatimes.com",
    "m.economictimes.com",
    "thehindu.com",
    "indianexpress.com",
    "business-standard.com",
    "pib.gov.in",
]

BLOCKED_DOMAIN_SUBSTRINGS = [
    "wikipedia.org",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "medium.com",
    "quora.com",
    "reddit.com",
    "github.com",
    "slideshare.net",
]
BLOCKED_URL_EXTENSIONS = [".pdf", ".ppt", ".pptx", ".doc", ".docx"]

EXPLAINER_PHRASES = [
    "what is",
    "meaning of",
    "definition of",
    "tutorial",
    "how to",
    "beginner",
    "introduction to",
    "syllabus",
    "mcq",
    "interview questions",
]

COMPANY_CONTEXT = """
You are filtering news for a semiconductor/electronics company operating in India.

Keep only genuine NEWS articles relevant to ANY of these:
A) Semiconductor/Electronics:
- Semiconductors / chips / fabs / foundries / OSAT / ATMP / packaging
- India Semiconductor Mission / Semicon India / ISM incentives/policies
- MeitY / Electronics & IT ministry announcements
- Ashwini Vaishnaw, S Krishnan (policy/industry announcements)
- India AI Mission (government or major industry moves)

B) India Trade/Policy/Economy (even if semiconductors are NOT mentioned):
- India trade news: tariffs, duties, exports/imports, FTAs, WTO disputes, trade pacts
- India policy news: cabinet decisions, bills/acts, regulations, schemes, govt notifications
- India economy news: RBI policy, repo rate, inflation (CPI/WPI), GDP, budget, fiscal deficit, PMI/IIP, rupee/forex reserves, FDI

Drop:
- explainers/definitions/tutorials
- Wikipedia/LinkedIn/social posts
- job posts, course pages, generic topic pages
- irrelevant content
"""

BATCH_SIZE = 8
MAX_BATCH_CHARS = 16000
USE_DUPLICATE_KEY_FROM_LLM = True


# ===============================
# UTIL
# ===============================
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def hash_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def preferred_domain_score(domain: str) -> int:
    d = (domain or "").lower()
    for i, p in enumerate(PREFERRED_DOMAINS):
        if p in d:
            return 100 - i
    return 0


def looks_blocked(url: str, domain: str, title: str, content: str) -> Tuple[bool, str]:
    u = (url or "").lower()
    d = (domain or "").lower()
    t = (title or "").lower()
    c = (content or "").lower()

    for ext in BLOCKED_URL_EXTENSIONS:
        if u.endswith(ext):
            return True, "blocked_filetype:%s" % ext

    for bad in BLOCKED_DOMAIN_SUBSTRINGS:
        if bad in u or bad in d:
            return True, "blocked_domain:%s" % bad

    blob = "%s %s" % (t, c)
    for p in EXPLAINER_PHRASES:
        if p in blob:
            return True, "explainer_phrase:%s" % p

    if len(norm_text(content)) < 50 and len(norm_text(title)) < 20:
        return True, "too_little_text"

    return False, ""


def parse_date_from_item(item: Dict[str, Any]) -> Optional[datetime]:
    candidates = [
        item.get("published_date"),
        item.get("published_dt"),
        item.get("publishedAt"),
        item.get("published_at"),
        item.get("published"),
        item.get("date"),
        item.get("time"),
        item.get("created_at"),
        item.get("created"),
        item.get("timestamp"),
    ]

    for v in candidates:
        if not v:
            continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(float(v), tz=tz.UTC).astimezone(LOCAL_TZ)

            if isinstance(v, str):
                dt = dtparser.parse(v, fuzzy=True)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=LOCAL_TZ)
                else:
                    dt = dt.astimezone(LOCAL_TZ)
                return dt
        except Exception:
            continue

    return None


def is_within_days(dt: datetime, days: int) -> bool:
    now = datetime.now(LOCAL_TZ)
    return dt >= (now - timedelta(days=days))


def canonicalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if len(u) > 10 and u.endswith("/"):
        u = u[:-1]
    u = re.sub(r"#.*$", "", u)
    return u


def simple_duplicate_key(title: str) -> str:
    t = norm_text(title)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    stop = set(["the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "as",
                "india", "indian", "latest", "today", "report", "news"])
    toks = [x for x in t.split() if x and x not in stop]
    toks = toks[:10]
    return " ".join(toks) or t[:60]


def dedupe_exact(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best_by_url: Dict[str, Dict[str, Any]] = {}
    for it in items:
        url = canonicalize_url(it.get("url", ""))
        if not url:
            continue
        it["url"] = url

        prev = best_by_url.get(url)
        if not prev:
            best_by_url[url] = it
        else:
            if len(it.get("content", "") or "") > len(prev.get("content", "") or ""):
                best_by_url[url] = it
    return list(best_by_url.values())


# ===============================
# GROQ LLM
# ===============================
def groq_chat(messages: List[Dict[str, str]]) -> str:
    headers = {
        "Authorization": "Bearer %s" % GROQ_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 2000,
    }
    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
    if not r.ok:
        raise RuntimeError("Groq error %s: %s" % (r.status_code, (r.text or "")[:700]))
    return (r.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def build_batches(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    batches = []
    cur = []
    cur_chars = 0

    for it in items:
        blob = json.dumps(
            {
                "id": it["_id"],
                "title": it.get("title", ""),
                "domain": it.get("domain", ""),
                "url": it.get("url", ""),
                "content": (it.get("content", "") or "")[:900],
                "published_dt": it.get("published_dt"),
            },
            ensure_ascii=False,
        )

        if cur and (len(cur) >= BATCH_SIZE or (cur_chars + len(blob)) > MAX_BATCH_CHARS):
            batches.append(cur)
            cur = []
            cur_chars = 0

        cur.append(it)
        cur_chars += len(blob)

    if cur:
        batches.append(cur)
    return batches


def llm_judge(items: List[Dict[str, Any]], cache: Dict[str, Any]) -> Dict[str, Any]:
    to_judge = [it for it in items if it["_id"] not in cache]
    if not to_judge:
        return cache

    batches = build_batches(to_judge)

    for bi, batch in enumerate(batches, start=1):
        payload_items = []
        for it in batch:
            payload_items.append(
                {
                    "id": it["_id"],
                    "title": it.get("title", ""),
                    "domain": it.get("domain", ""),
                    "url": it.get("url", ""),
                    "published_dt": it.get("published_dt"),
                    "content": (it.get("content", "") or "")[:900],
                }
            )

        prompt = """
%s

For each item, decide:
- is_news: true only if it is a news report (NOT explainer/definition/tutorial/profile page)
- keep: true only if it is relevant news for my company context
- relevance_score: integer 0-10 (10 = must-read)
- reason: short reason (max 12 words)
- duplicate_key: same story across sites must share same key (lowercase, stable)
- You MUST return one output object for EVERY input id.
- If uncertain, still return the id with keep=false, is_news=false, relevance_score=0.

Return STRICT JSON ONLY in this schema:
{
  "items": [
    {
      "id":"...",
      "keep":true/false,
      "is_news":true/false,
      "relevance_score":0-10,
      "reason":"...",
      "duplicate_key":"..."
    }
  ]
}

Items:
%s
""".strip() % (COMPANY_CONTEXT, json.dumps(payload_items, ensure_ascii=False))

        messages = [
            {"role": "system", "content": "Output ONLY valid JSON. No extra text."},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(1, 4):
            try:
                raw = groq_chat(messages)
                data = json.loads(raw)
                out_items = data.get("items", [])
                got = {x.get("id"): x for x in out_items if x.get("id")}

                for it in batch:
                    rid = it["_id"]
                    if rid in got:
                        x = got[rid]
                        cache[rid] = {
                            "id": rid,
                            "keep": bool(x.get("keep", False)),
                            "is_news": bool(x.get("is_news", False)),
                            "relevance_score": int(x.get("relevance_score", 0) or 0),
                            "reason": (x.get("reason") or "").strip(),
                            "duplicate_key": (x.get("duplicate_key") or "").strip().lower(),
                        }
                    else:
                        cache[rid] = {
                            "id": rid,
                            "keep": False,
                            "is_news": False,
                            "relevance_score": 0,
                            "reason": "LLM missing judgement",
                            "duplicate_key": "",
                        }
                break
            except Exception as e:
                logger.exception("LLM call failed attempt %d", attempt)
                if attempt == 3:
                    for it in batch:
                        rid = it["_id"]
                        cache[rid] = {
                            "id": rid,
                            "keep": False,
                            "is_news": False,
                            "relevance_score": 0,
                            "reason": "LLM error: %s" % type(e).__name__,
                            "duplicate_key": "",
                        }
                else:
                    time.sleep(1.5 * attempt)

        logger.info("LLM judged batch %d/%d (%d items)", bi, len(batches), len(batch))
        time.sleep(0.4)

    return cache


def dedupe_by_story(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        llm = it.get("llm", {})
        dk = (llm.get("duplicate_key") or "").strip().lower()
        if not dk or dk == "null":
            dk = simple_duplicate_key(it.get("title", ""))
        clusters.setdefault(dk, []).append(it)

    final: List[Dict[str, Any]] = []

    for dk, group in clusters.items():
        def score(x: Dict[str, Any]) -> float:
            llm = x.get("llm", {})
            rel = float(llm.get("relevance_score", 0) or 0)
            dom = float(preferred_domain_score(x.get("domain", "")) / 100.0)
            has_date = 0.6 if x.get("published_dt") else 0.0
            content_bonus = min(len(x.get("content", "") or ""), 1200) / 2000.0
            return rel + dom + has_date + content_bonus

        best = sorted(group, key=score, reverse=True)[0]
        final.append(best)

    return final


def main():
    logger.info("Processor started")
    items = load_json(INPUT_FILE, default=[])
    if not isinstance(items, list):
        raise RuntimeError("%s must be a JSON list" % INPUT_FILE)

    normalized: List[Dict[str, Any]] = []
    for it in items:
        title = it.get("title", "") or ""
        url = canonicalize_url(it.get("url", "") or "")
        domain = (it.get("domain") or it.get("source") or "").strip().lower()
        content = it.get("content", "") or it.get("snippet", "") or ""

        dt = parse_date_from_item(it)

        _id = hash_key(url.lower(), norm_text(title))
        normalized.append(
            {
                "_id": _id,
                "title": title,
                "url": url,
                "domain": domain,
                "content": content,
                "keyword_hits": it.get("keyword_hits", []),
                "published_dt": dt.isoformat() if dt else None,
            }
        )

    normalized = dedupe_exact(normalized)
    logger.info("After exact URL dedupe: %d items", len(normalized))

    hard_filtered = []
    hard_rejected = []

    for it in normalized:
        blocked, reason = looks_blocked(it.get("url",""), it.get("domain",""), it.get("title",""), it.get("content",""))
        if blocked:
            it["hard_reject_reason"] = reason
            hard_rejected.append(it)
        else:
            hard_filtered.append(it)

    logger.info("After hard non-news filter: %d items", len(hard_filtered))
    logger.info("Hard rejected (non-news): %d items", len(hard_rejected))

    recent = []
    old_rejected = []

    for it in hard_filtered:
        if not it.get("published_dt"):
            if KEEP_IF_DATE_UNKNOWN:
                recent.append(it)
            else:
                it["hard_reject_reason"] = "missing_date"
                old_rejected.append(it)
            continue

        try:
            dt = dtparser.parse(it["published_dt"])
        except Exception:
            if KEEP_IF_DATE_UNKNOWN:
                recent.append(it)
            else:
                it["hard_reject_reason"] = "bad_date_parse"
                old_rejected.append(it)
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt = dt.astimezone(LOCAL_TZ)

        if is_within_days(dt, DAYS):
            recent.append(it)
        else:
            it["hard_reject_reason"] = "older_than_%d_days" % DAYS
            old_rejected.append(it)

    logger.info("After date filter (last %d days, keep unknown): %d items", DAYS, len(recent))
    logger.info("Date rejected (known & old): %d items", len(old_rejected))

    cache = load_json(LLM_CACHE_FILE, default={})
    if not isinstance(cache, dict):
        cache = {}

    cache = llm_judge(recent, cache)
    save_json(LLM_CACHE_FILE, cache)
    logger.info("LLM cache saved: %s", LLM_CACHE_FILE)

    accepted: List[Dict[str, Any]] = []
    rejected_llm: List[Dict[str, Any]] = []

    for it in recent:
        d = cache.get(it["_id"], {})
        keep = bool(d.get("keep", False))
        is_news = bool(d.get("is_news", False))
        score = int(d.get("relevance_score", 0) or 0)

        it["llm"] = {
            "keep": keep,
            "is_news": is_news,
            "relevance_score": score,
            "reason": d.get("reason", ""),
            "duplicate_key": d.get("duplicate_key", "") if USE_DUPLICATE_KEY_FROM_LLM else "",
        }

        if keep and is_news and score >= MIN_RELEVANCE_SCORE:
            accepted.append(it)
        else:
            rejected_llm.append(it)

    logger.info("LLM accepted (pre story-dedupe): %d", len(accepted))
    logger.info("LLM rejected: %d", len(rejected_llm))

    accepted = dedupe_by_story(accepted)
    logger.info("After story dedupe: %d", len(accepted))

    def sort_key(x: Dict[str, Any]) -> Tuple[int, int]:
        llm = x.get("llm", {})
        score = int(llm.get("relevance_score", 0) or 0)
        dt2 = None
        if x.get("published_dt"):
            try:
                dt2 = dtparser.parse(x["published_dt"])
            except Exception:
                dt2 = None
        ts = int(dt2.timestamp()) if dt2 else 0
        return (ts, score)

    accepted.sort(key=sort_key, reverse=True)

    save_json(OUTPUT_JSON, accepted)

    rejected_all = []
    rejected_all.extend(hard_rejected)
    rejected_all.extend(old_rejected)
    rejected_all.extend(rejected_llm)
    rejected_all = dedupe_exact(rejected_all)
    save_json(REJECTED_JSON, rejected_all)

    # txt outputs
    ok_lines = ["Filtered relevant news (last %d days) — %d items\n" % (DAYS, len(accepted))]
    for it in accepted:
        ok_lines.append("- Title: %s" % it.get("title",""))
        ok_lines.append("  Source: %s" % it.get("domain",""))
        ok_lines.append("  URL: %s" % it.get("url",""))
        ok_lines.append("  Published: %s" % (it.get("published_dt") or "N/A"))
        hits = it.get("keyword_hits") or []
        if hits:
            ok_lines.append("  Tags: %s" % ", ".join(hits))
        llm = it.get("llm", {})
        ok_lines.append("  Relevance: %s/10 — %s" % (llm.get("relevance_score",0), llm.get("reason","")))
        ok_lines.append("")

    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(ok_lines))

    bad_lines = ["Rejected items — %d items\n" % len(rejected_all)]
    for it in rejected_all:
        bad_lines.append("- Title: %s" % it.get("title",""))
        bad_lines.append("  Source: %s" % it.get("domain",""))
        bad_lines.append("  URL: %s" % it.get("url",""))
        bad_lines.append("  Published: %s" % (it.get("published_dt") or "N/A"))
        if it.get("hard_reject_reason"):
            bad_lines.append("  Hard Reject: %s" % it.get("hard_reject_reason"))
        if it.get("llm"):
            llm = it.get("llm", {})
            bad_lines.append("  LLM: keep=%s news=%s score=%s reason=%s"
                             % (llm.get("keep"), llm.get("is_news"), llm.get("relevance_score"), llm.get("reason")))
        bad_lines.append("")

    with open(REJECTED_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(bad_lines))

    logger.info("Saved: %s, %s, %s, %s", OUTPUT_JSON, OUTPUT_TXT, REJECTED_JSON, REJECTED_TXT)
    logger.info("Processor finished successfully")


if __name__ == "__main__":
    main()
