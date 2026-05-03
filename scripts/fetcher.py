#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import requests
from typing import List, Dict, Any, Tuple, Optional

# =========================
# KEYS (HARDCODED) - move to env vars later
# =========================
TAVILY_KEY = ""
GROQ_API_KEY = ""

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

DAYS = 7
SEARCH_DEPTH = "advanced"
MAX_RESULTS_PER_CALL = 50

FETCH_DATE_FROM_URL = True
HTTP_TIMEOUT = 18
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
HTML_MAX_CHARS = 600000

# =========================
# OUTPUT DIRECTORY (FIXED)
# Writes always to: NXP_autonews/data/
# =========================
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))   # .../scripts
BASE_DIR = os.path.dirname(SCRIPTS_DIR)                    # .../NXP_autonews
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MATCHED_JSON_PATH = os.path.join(DATA_DIR, "matched_news.json")
RAW_TXT_PATH = os.path.join(DATA_DIR, "raw_news_data.txt")

# -------------------------------
# 1) KEYWORD GROUPS
# -------------------------------
KEYWORD_GROUPS = [
    ("Semiconductors",
     r"\b(semiconductor|semiconductors|semicon|chip|chips|ic|ics|silicon|fab|fabs|foundry|wafer|wafers|"
     r"osat|atmp|assembly|test|packaging|advanced packaging)\b"),

    ("India Semiconductor Mission",
     r"\b(india semiconductor mission|ism|semicon india|semiconindia)\b"),

    ("MeitY / Ministry of Electronics and IT",
     r"\b(meity|ministry of electronics( and| &)? (it|information technology)|"
     r"electronics( and| &)? it ministry|ministry of electronics)\b"),

    ("Ashwini Vaishnaw",
     r"\bashwini vaishnaw\b"),

    ("S Krishnan",
     r"\b(s\.?\s*krishnan|s\s*krishnan)\b"),

    ("India AI Mission",
     r"\b(india ai mission|indiaai|ai mission)\b"),

    ("India Trade",
     r"\b(trade|fta|export|exports|import|imports|tariff|tariffs|duty|duties)\b"),

    ("Electronics Manufacturing / Policy",
     r"\b(electronics manufacturing|p(li)? scheme|production linked incentive|incentive|subsidy|policy|cabinet)\b"),
]

KEYWORD_GROUPS += [
    ("India Trade News",
     r"\b(india|indian)\b.*\b(trade|fta|free trade agreement|wto|export|exports|import|imports|tariff|tariffs|"
     r"duty|duties|customs|anti-dumping|countervailing|trade pact|trade deal|trade deficit)\b"
     r"|\b(trade|fta|free trade agreement|wto|export|exports|import|imports|tariff|tariffs|duty|duties|customs|"
     r"anti-dumping|countervailing|trade pact|trade deal|trade deficit)\b.*\b(india|indian)\b"),

    ("India Policy News",
     r"\b(india|indian)\b.*\b(policy|cabinet|bill|act|amendment|notification|gazette|regulation|rules|guidelines|"
     r"scheme|mission|incentive|subsidy|ministry|government|parliament|ordinance)\b"
     r"|\b(policy|cabinet|bill|act|amendment|notification|gazette|regulation|rules|guidelines|scheme|mission|"
     r"incentive|subsidy|ministry|government|parliament|ordinance)\b.*\b(india|indian)\b"),

    ("India Economy News",
     r"\b(india|indian)\b.*\b(gdp|growth|inflation|cpi|wpi|fiscal|fiscal deficit|budget|union budget|interim budget|"
     r"rbi|repo rate|monetary policy|mpc|forex|fx reserves|rupee|inr|bond yield|pmi|iip|unemployment|jobs|fdi|"
     r"current account|cad)\b"
     r"|\b(gdp|growth|inflation|cpi|wpi|fiscal|fiscal deficit|budget|union budget|interim budget|rbi|repo rate|"
     r"monetary policy|mpc|forex|fx reserves|rupee|inr|bond yield|pmi|iip|unemployment|jobs|fdi|current account|cad)\b"
     r".*\b(india|indian)\b"),
]

# -------------------------------
# 2) SEARCH QUERIES
# -------------------------------
QUERY_PLAN = [
    'india semiconductor news',
    'semiconductors news',
    'semiconductor india news',
    'semicon news',
    'chip news',
    'semiconductor India investment OR policy',
    'semicon India announced OR investment OR policy',
    'chip fab foundry India announced OR approved',
    'OSAT OR ATMP India semiconductor announced OR approved',
    'semiconductor India MeitY OR ISM OR "Semicon India"',

    '"India Semiconductor Mission" OR ISM latest',
    '"Semicon India" OR semiconindia latest',
    '"Semicon India" incentives OR ISM policy OR cabinet approval',

    'MeitY semiconductor India announced OR approved',
    '"Ministry of Electronics and IT" semiconductor India announced',
    '"Ashwini Vaishnaw" semiconductor OR semicon OR chip OR fab',
    '"Ashwini Vaishnaw" MeitY policy OR approval',

    '"S Krishnan" MeitY semiconductor',
    '"S. Krishnan" "Ministry of Electronics"',

    '"India AI Mission" announcement OR funding OR policy',
    'IndiaAI mission launch OR approval OR ministry',

    'India trade semiconductor export import tariff',
    'FTA India electronics semiconductor trade',
	'india semiconductor news economic times',
	'india semiconductor news business standard',

    'electronics manufacturing India PLI semiconductor',
    'chip design India semiconductor policy'
	
	'economictimes.indiatimes.com semiconductor news',
    'm.economictimes.com semiconductor news',
    'telecom.economictimes.indiatimes.com semiconductor news',
    'thehindu.com semiconductor news',
    'indianexpress.com semiconductor news',
    'business-standard.com semiconductor news',
    'moneycontrol.com semiconductor news',
    'businesstoday.in semiconductor news',
    'reuters.com semiconductor news',
    'bloomberg.com semiconductor news',
]

QUERY_PLAN += [
    'India semiconductor policy news',
	'India electronics policy',
	'India semiconductor policy news',
	'India semiconductor trade news',
	'India electronics trade news',
    'India trade news export import tariff',
    'India FTA trade deal news',
    'India customs duty tariff notification',
    'India WTO trade dispute',
    'India anti-dumping duty latest',

    'India cabinet approval policy scheme',
    'India government notification new rules policy',
    'India bill passed new act amendment',
    'India ministry announces new policy scheme',

    'India economy news GDP inflation',
    'India RBI monetary policy repo rate',
    'India budget fiscal deficit announcement',
    'India rupee forex reserves update',
    'India PMI IIP data release',
    'India FDI inflows latest',
]

EXCLUDE_TERMS = ' -("what is" OR meaning OR definition OR explained OR tutorial OR guide OR wikipedia OR syllabus OR ppt OR pdf)'

# -------------------------------
# DATE EXTRACTION
# -------------------------------
META_DATE_PATTERNS = [
    r'property="article:published_time"\s+content="([^"]+)"',
    r'property="article:modified_time"\s+content="([^"]+)"',
    r'property="og:updated_time"\s+content="([^"]+)"',
    r'name="publish-date"\s+content="([^"]+)"',
    r'name="pubdate"\s+content="([^"]+)"',
    r'name="date"\s+content="([^"]+)"',
    r'name="dc\.date"\s+content="([^"]+)"',
    r'name="dc\.date\.issued"\s+content="([^"]+)"',
    r'itemprop="datePublished"\s+content="([^"]+)"',
    r'itemprop="dateModified"\s+content="([^"]+)"',
]
JSON_DATE_KEYS = ["datePublished", "dateCreated", "dateModified", "publishedAt", "pubDate", "publishDate"]
ISO_DATE_ANYWHERE = r"\b(20\d{2}[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])(?:[T\s][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:\.\d+)?(?:Z|[+-][0-2]\d:?[0-5]\d)?)?)\b"


def tavily_search(query: str, search_depth: str, max_results: int, days: int) -> Dict[str, Any]:
    """
    Python 3.6 compatible Tavily REST call (no tavily-python SDK needed).
    """
    payload = {
        "api_key": TAVILY_KEY,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "days": days,
        "include_answer": False,
        "include_raw_content": False,
    }
    r = requests.post(TAVILY_SEARCH_URL, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError("Tavily error %s: %s" % (r.status_code, (r.text or "")[:500]))
    return r.json() if (r.text or "").strip() else {}


def fetch_html(url: str) -> str:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if not r.ok:
            return ""
        return (r.text or "")[:HTML_MAX_CHARS]
    except Exception:
        return ""


def extract_date_from_html(html: str) -> Optional[str]:
    if not html:
        return None

    for pat in META_DATE_PATTERNS:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()

    m = re.search(r'(?is)<time[^>]+datetime="([^"]+)"', html)
    if m:
        return m.group(1).strip()

    blocks = re.findall(r'(?is)<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html)
    for block in blocks[:12]:
        for k in JSON_DATE_KEYS:
            m2 = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(k), block)
            if m2:
                return m2.group(1).strip()

    m = re.search(ISO_DATE_ANYWHERE, html)
    if m:
        return m.group(1).strip()

    return None


def refine_with_groq(prompt: str) -> str:
    headers = {
        "Authorization": "Bearer %s" % GROQ_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Format-only. Do not add/remove items. Do not invent dates."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError("Groq error %s: %s" % (r.status_code, (r.text or "")[:500]))
    return ((r.json() or {}).get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def match_groups(text: str) -> List[str]:
    hits = []
    for label, pattern in KEYWORD_GROUPS:
        if re.search(pattern, text or "", flags=re.IGNORECASE):
            hits.append(label)
    return hits


def dedupe_by_url(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        u = (it.get("url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(it)
    return out


def tavily_date(r: Dict[str, Any]) -> Optional[str]:
    return r.get("published_date") or r.get("published_time") or r.get("published") or r.get("date") or None


def fetch_all_matching_news() -> None:
    print("FETCHER STARTED", flush=True)
    print("DATA_DIR=%s" % DATA_DIR, flush=True)

    print("?? Step 1: Running multiple Tavily searches (REST)...", flush=True)
    all_results: List[Dict[str, Any]] = []

    for i, base_q in enumerate(QUERY_PLAN, start=1):
        q = "(%s)%s" % (base_q, EXCLUDE_TERMS)
        print("  ?? Query %d/%d: %s" % (i, len(QUERY_PLAN), base_q), flush=True)

        data = tavily_search(
            query=q,
            search_depth=SEARCH_DEPTH,
            max_results=MAX_RESULTS_PER_CALL,
            days=DAYS,
        )
        if isinstance(data, dict):
            all_results.extend(data.get("results", []) or [])

    results = dedupe_by_url(all_results)
    print("? Total unique URLs from search: %d" % len(results), flush=True)

    print("?? Step 2: Matching keywords in TITLE + TAVILY CONTENT only...", flush=True)
    matched: List[Dict[str, Any]] = []

    for idx, r in enumerate(results, start=1):
        title = r.get("title", "") or ""
        snippet = r.get("content", "") or ""
        url = r.get("url", "") or ""
        domain = r.get("domain", "") or ""

        combined = "%s\n%s" % (title, snippet)
        hits = match_groups(combined)

        if hits:
            pub = tavily_date(r)

            if (not pub) and FETCH_DATE_FROM_URL and url:
                html = fetch_html(url)
                pub = extract_date_from_html(html)

            matched.append({
                "title": title,
                "url": url,
                "domain": domain,
                "content": snippet,
                "published_date": pub,
                "keyword_hits": hits,
            })

        if idx % 50 == 0:
            print("   ? Processed %d/%d | matched so far: %d" % (idx, len(results), len(matched)), flush=True)

    matched = dedupe_by_url(matched)
    print("? Matched (unique): %d" % len(matched), flush=True)

    with open(MATCHED_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=2)

    prompt = """
Format these items into a clean plain-text report.
Rules:
- Do NOT add/remove items.
- Do NOT invent dates.
- Show: Title, Published Date (if available), Source(domain), URL, Matched Keywords.
- Use ONLY the provided fields.

DATA (JSON):
%s
""".strip() % json.dumps(matched, ensure_ascii=False)

    try:
        report = refine_with_groq(prompt)
    except Exception as e:
        print("?? Groq formatting failed: %s" % str(e), flush=True)
        report = "\n\n".join(
            [
                "- Title: %s\n  Date: %s\n  Source: %s\n  URL: %s\n  Matched: %s\n"
                % (
                    x.get("title", ""),
                    x.get("published_date") or "N/A",
                    x.get("domain", ""),
                    x.get("url", ""),
                    ", ".join(x.get("keyword_hits") or []),
                )
                for x in matched
            ]
        )

    with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print("? Saved %s and %s" % (MATCHED_JSON_PATH, RAW_TXT_PATH), flush=True)


if __name__ == "__main__":
    fetch_all_matching_news()
