<%@ page contentType="text/html; charset=UTF-8" pageEncoding="UTF-8" isELIgnored="true" %>
<%@ page import="java.nio.file.*, java.nio.charset.StandardCharsets" %>
<%@ page import="java.io.File" %>

<%!
  private String escapeForJsSingleQuotedString(String s) {
    if (s == null) return "";
    StringBuilder out = new StringBuilder(s.length() + 64);
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '\\': out.append("\\\\"); break;
        case '\'': out.append("\\'"); break;
        case '\r': out.append("\\r"); break;
        case '\n': out.append("\\n"); break;
        case '\t': out.append("\\t"); break;
        case '<':  out.append("\\u003C"); break;
        case '>':  out.append("\\u003E"); break;
        case '&':  out.append("\\u0026"); break;
        default:   out.append(c);
      }
    }
    return out.toString();
  }
%>

<%
  String jsonPath = application.getRealPath("/data/final_news.json");
  String rawJson = "[]";
  long lastModified = 0L;
  boolean fileExists = false;
  String fileError = null;

  try {
    if (jsonPath != null) {
      File f = new File(jsonPath);
      fileExists = f.exists() && f.isFile();
      if (fileExists) {
        lastModified = f.lastModified();
        byte[] bytes = Files.readAllBytes(f.toPath());
        rawJson = new String(bytes, StandardCharsets.UTF_8);
        if (rawJson.trim().isEmpty()) rawJson = "[]";
      }
    } else {
      fileError = "application.getRealPath returned null (WAR may be not expanded).";
    }
  } catch (Exception e) {
    fileError = e.toString();
    rawJson = "[]";
  }

  String rawJsonEscaped = escapeForJsSingleQuotedString(rawJson);
%>

<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AutoNews</title>

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

  <style>
    body { background: #f7f8fb; }
    .brand { letter-spacing: .2px; }
    .subtle { color: #6b7280; }
    .cardx { border: 1px solid rgba(17,24,39,.08); box-shadow: 0 6px 22px rgba(17,24,39,.06); border-radius: 16px; background: #fff; }
    .cardx:hover { box-shadow: 0 10px 32px rgba(17,24,39,.10); transform: translateY(-1px); transition: .15s ease; }
    .title {
      font-size: 1.02rem;
      line-height: 1.25rem;
      margin: 0;
      font-weight: 650;
      color: #0f172a;
      word-break: break-word;
    }
    .meta { font-size: .86rem; color: #475569; }
    .badge-soft {
      background: rgba(2,6,23,.06);
      color: #0f172a;
      border: 1px solid rgba(2,6,23,.08);
      font-weight: 600;
    }
    .tag {
      background: rgba(59,130,246,.10);
      color: #1e40af;
      border: 1px solid rgba(59,130,246,.18);
      font-weight: 600;
    }
    .muted-link { text-decoration: none; color: inherit; }
    .muted-link:hover { text-decoration: underline; }
    .toolbar .form-control, .toolbar .form-select { border-radius: 12px; }
    .btn { border-radius: 12px; }
    .empty {
      border: 1px dashed rgba(2,6,23,.18);
      border-radius: 16px;
      padding: 24px;
      color: #334155;
      background: #fff;
    }
    .small-note { font-size: .82rem; }
    pre.summary-box {
      white-space: pre-wrap;
      background: #f8fafc;
      border: 1px solid rgba(2,6,23,.10);
      padding: 10px;
      border-radius: 12px;
      margin: 0;
    }
    code { font-size: .9em; }
  </style>
</head>

<body>
  <div class="container py-4">

    <!-- Header -->
    <div class="d-flex flex-column flex-md-row align-items-md-center justify-content-between gap-3 mb-3">
      <div>
        <h1 class="h4 mb-0 brand">AutoNews</h1>
      </div>

      <div class="text-md-end">
        <div class="subtle small-note">
          Last updated:
          <% if (fileExists) { %>
            <span id="lastUpdated"></span>
          <% } else { %>
            <span class="text-danger">N/A</span>
          <% } %>
        </div>
        <div class="subtle small-note">
          Items shown: <span id="countShown">0</span> / <span id="countTotal">0</span>
        </div>
      </div>
    </div>

    <% if (fileError != null) { %>
      <div class="alert alert-danger">
        Failed to read <code>final_news.json</code>: <%= fileError %>
        <div class="mt-2 small-note">
          Resolved path: <code><%= (jsonPath == null ? "null" : jsonPath) %></code>
        </div>
      </div>
    <% } %>

    <!-- Toolbar -->
    <div class="cardx p-3 mb-3 toolbar">
      <div class="row g-2 align-items-center">
        <div class="col-12 col-md-5">
          <input id="q" class="form-control" placeholder="Search title, tags, domain" />
        </div>
        <div class="col-6 col-md-3">
          <select id="domain" class="form-select">
            <option value="">All domains</option>
          </select>
        </div>
        <div class="col-6 col-md-3">
          <select id="sort" class="form-select">
            <option value="date_desc">Sort: Date (New to Old)</option>
            <option value="date_asc">Sort: Date (Old to New)</option>
            <option value="title_asc">Sort: Title (A to Z)</option>
          </select>
        </div>
        <div class="col-12 col-md-1 d-grid">
          <button id="clear" class="btn btn-outline-secondary">Clear</button>
        </div>
      </div>
    </div>

    <!-- Content -->
    <div id="list" class="row g-3"></div>

    <div id="empty" class="empty mt-3 d-none">
      <div class="fw-semibold mb-1">No items found.</div>
    </div>
  </div>

  <script>
    const SUMMARIZE_API = "http://10.224.148.39:8096/summarize";

    const RAW_JSON = '<%= rawJsonEscaped %>';
    const FILE_EXISTS = <%= fileExists ? "true" : "false" %>;
    const LAST_MOD_MS = <%= lastModified %>;

    let ITEMS = [];
    try {
      ITEMS = JSON.parse(RAW_JSON);
      if (!Array.isArray(ITEMS)) ITEMS = [];
    } catch (e) {
      ITEMS = [];
      console.error("JSON parse failed:", e);
    }

    const $ = (id) => document.getElementById(id);

    function safeText(x) { return (x === null || x === undefined) ? "" : String(x); }

    function parseDateMs(it) {
      const s = it.published_dt || it.published_date || it.publishedAt || it.date || "";
      const t = Date.parse(s);
      return Number.isFinite(t) ? t : 0;
    }

    function domainOf(it) { return (it.domain || "").toLowerCase(); }

    function tagsOf(it) {
      const tags = it.keyword_hits || it.tags || [];
      if (!Array.isArray(tags)) return [];
      return tags.map(safeText).filter(Boolean);
    }

    function formatDate(ms) {
      if (!ms) return "N/A";
      return new Date(ms).toLocaleString();
    }

    function escapeHtml(s) {
      return safeText(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll("\"", "&quot;")
        .replaceAll("'", "&#039;");
    }

    // CHANGE 1: summarizeArticle now accepts a fallback summary from the dataset
    async function summarizeArticle(cardId, url, fallback) {
      const box = document.getElementById(cardId);
      const pre = box.querySelector("pre");
      box.classList.remove("d-none");
      pre.textContent = "Summarizing…";

      try {
        const res = await fetch(SUMMARIZE_API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url })
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const summary = (data.summary || "").trim();
        if (!summary) throw new Error("Empty summary");
        pre.textContent = summary;
      } catch (e) {
        // CHANGE 2: on any failure, use summary_4l from the dataset if available
        if (fallback && fallback.trim()) {
          pre.textContent = fallback;
        } else {
          pre.textContent = "Failed to summarize: " + (e?.message || String(e));
        }
      }
    }

    if (FILE_EXISTS && LAST_MOD_MS) $("lastUpdated").textContent = formatDate(LAST_MOD_MS);
    $("countTotal").textContent = String(ITEMS.length);

    const domains = Array.from(new Set(ITEMS.map(domainOf).filter(Boolean))).sort();
    const domainSel = $("domain");
    for (const d of domains) {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      domainSel.appendChild(opt);
    }

    function render(list) {
      const container = $("list");
      container.innerHTML = "";

      $("countShown").textContent = String(list.length);
      $("empty").classList.toggle("d-none", list.length !== 0);

      for (const it of list) {
        const title = safeText(it.title);
        const url = safeText(it.url);
        const dom = domainOf(it) || "unknown";
        const dateMs = parseDateMs(it);
        const tags = tagsOf(it);
        const fallback = safeText(it.summary_4l);

        const col = document.createElement("div");
        col.className = "col-12 col-lg-6";

        const cardId = "sum_" + Math.random().toString(36).slice(2);

        col.innerHTML = `
          <div class="cardx p-3 h-100">
            <div class="d-flex align-items-start justify-content-between gap-2">
              <div class="flex-grow-1">
                <h3 class="title">
                  <a class="muted-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">
                    ${escapeHtml(title)}
                  </a>
                </h3>

                <div class="meta mt-2">
                  <span class="badge badge-soft me-1">${escapeHtml(dom)}</span>
                  <span class="badge badge-soft">Date: ${escapeHtml(formatDate(dateMs))}</span>
                </div>
              </div>

              <div class="d-flex flex-column gap-2">
                <a class="btn btn-primary btn-sm" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Open</a>
                <!-- CHANGE 3: Summarize button uses data-* attrs to safely pass url and fallback, no inline escaping issues -->
                <button class="btn btn-success btn-sm summarize-btn" type="button"
                        data-card-id="${cardId}"
                        data-url="${escapeHtml(url)}">
                  Summarize
                </button>
              </div>
            </div>

            <div id="${cardId}" class="mt-2 d-none">
              <div class="subtle small-note mb-1 fw-semibold">Summary (4 points)</div>
              <pre class="summary-box small-note"></pre>
            </div>

            ${tags.length ? `
              <div class="d-flex flex-wrap gap-2 mt-3">
                ${tags.slice(0, 10).map(t => `<span class="badge tag">${escapeHtml(t)}</span>`).join("")}
              </div>
            ` : ""}
          </div>
        `;

        // Set fallback via JS property to avoid any HTML escaping concerns
        col.querySelector(".summarize-btn").dataset.fallback = fallback;

        // Attach click listener directly — no onclick string needed
        col.querySelector(".summarize-btn").addEventListener("click", function () {
          summarizeArticle(this.dataset.cardId, this.dataset.url, this.dataset.fallback);
        });

        container.appendChild(col);
      }
    }

    function applyFilters() {
      const q = safeText($("q").value).trim().toLowerCase();
      const dom = safeText($("domain").value).trim().toLowerCase();
      const sort = safeText($("sort").value);

      let list = ITEMS.slice();

      if (dom) list = list.filter(it => domainOf(it) === dom);

      if (q) {
        list = list.filter(it => {
          const title = safeText(it.title).toLowerCase();
          const d = domainOf(it);
          const tags = tagsOf(it).join(" ").toLowerCase();
          return title.includes(q) || d.includes(q) || tags.includes(q);
        });
      }

      list.sort((a, b) => {
        if (sort === "date_desc") return parseDateMs(b) - parseDateMs(a);
        if (sort === "date_asc") return parseDateMs(a) - parseDateMs(b);
        if (sort === "title_asc") return safeText(a.title).localeCompare(safeText(b.title));
        return 0;
      });

      render(list);
    }

    $("q").addEventListener("input", applyFilters);
    $("domain").addEventListener("change", applyFilters);
    $("sort").addEventListener("change", applyFilters);

    $("clear").addEventListener("click", () => {
      $("q").value = "";
      $("domain").value = "";
      $("sort").value = "date_desc";
      applyFilters();
    });

    applyFilters();
  </script>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
