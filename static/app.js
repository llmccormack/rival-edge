/* ==========================================================================
   Rival Edge — client
   Submits documents, renders the NDJSON progress stream, draws result cards,
   and handles PDF export.
   ========================================================================== */

(function () {
  "use strict";

  // ------------------------------------------------------------------ helpers

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const escapeHtml = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const slug = (value) => String(value || "").toLowerCase().replace(/[^a-z]/g, "");

  // Matches report.py's typography so the screen and the PDF read alike.
  const smartQuotes = (value) => String(value == null ? "" : value)
    .replace(/(\w)'(\w)/g, "$1\u2019$2")
    .replace(/"([^"\n]{1,400})"/g, "\u201c$1\u201d")
    .replace(/(^|[^\w'])'([^'\n]{1,400})'(?!\w)/g, "$1\u2018$2\u2019");

  const prose = (value) => escapeHtml(smartQuotes(value));

  const compactNumber = (value) => value >= 1000
    ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`
    : String(value);

  const MODEL_LABELS = { "claude-opus-5": document.body.dataset.modelLabel || "Claude Opus 5" };

  // The static GitHub Pages build has no server: samples and saved examples
  // are plain files, and live analysis is unavailable.
  const DEMO = document.body.dataset.demo === "true";
  const SAMPLE_FILES = {
    q2: "northwind-q2-fy2025-earnings-call.txt",
    q3: "northwind-q3-fy2025-earnings-call.txt"
  };
  const sampleUrl = (name) => DEMO ? `samples/${SAMPLE_FILES[name]}` : `/api/samples/${name}`;
  const exampleUrl = (kind) => DEMO ? `samples/example-${kind}.json` : `/api/examples/${kind}`;

  const ICONS = {
    building: '<path d="M3 21h18"/><path d="M5 21V5a2 2 0 012-2h6a2 2 0 012 2v16"/><path d="M15 21V9h4a2 2 0 012 2v10"/><path d="M9 7h2M9 11h2M9 15h2"/>',
    revenue: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    growth: '<path d="M22 7l-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    compass: '<circle cx="12" cy="12" r="10"/><path d="M16.2 7.8l-2.5 6.4-6.4 2.5 2.5-6.4z"/>',
    risk: '<path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L14.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    opportunity: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    quote: '<path d="M3 21c3 0 7-1 7-8V5a2 2 0 00-2-2H4a2 2 0 00-2 2v6a2 2 0 002 2h3"/><path d="M14 21c3 0 7-1 7-8V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v6a2 2 0 002 2h3"/>',
    scale: '<path d="M12 3v18"/><path d="M6 8H3v8h3z"/><path d="M21 6h-3v12h3z"/>',
    eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    check: '<path d="M20 6L9 17l-5-5"/>'
  };

  const icon = (name, size) => `<svg width="${size || 16}" height="${size || 16}" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

  const card = (iconName, title, bodyHtml, wide) => `
    <article class="card${wide ? " card--wide" : ""}">
      <div class="card__head">
        <span class="card__icon">${icon(iconName)}</span>
        <h3 class="card__title">${escapeHtml(title)}</h3>
      </div>
      <div class="card__body">${bodyHtml}</div>
    </article>`;

  const rankedList = (items) => `<ol class="ranked">${
    (items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")
  }</ol>`;

  const dataTable = (headers, rows) => `
    <div class="table-scroll">
      <table class="ledger">
        <thead><tr>${headers.map((h, i) => `
          <th${i ? ' class="ledger__num"' : ""}>${escapeHtml(h)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${row.map((cell, i) => `
          <td${i ? ' class="ledger__num"' : ""}>${prose(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>`;

  const quoteBlock = (entry) => `
    <blockquote class="quote">
      <p>“${prose(entry.quote)}”</p>
      <cite>${escapeHtml(entry.speaker)}${entry.context ? `
        <span class="quote__context quote__context--${slug(entry.context)}">${escapeHtml(entry.context)}</span>` : ""}</cite>
    </blockquote>`;

  const plainList = (items, emptyText) => (items && items.length)
    ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : `<p class="empty">${escapeHtml(emptyText)}</p>`;

  // --------------------------------------------------------------- state

  let mode = "single";
  let lastResult = null;
  let analysisResult = null;
  let memoResult = null;
  const files = {};

  // ---------------------------------------------------------------- tabs

  const setMode = (next) => {
    mode = next;
    $("#tab-single").setAttribute("aria-selected", String(next === "single"));
    $("#tab-compare").setAttribute("aria-selected", String(next === "compare"));
    $("#pane-single").hidden = next !== "single";
    $("#pane-compare").hidden = next === "single";
    $("#run-label").textContent = next === "single" ? "Analyze document" : "Compare quarters";
    $("#run-hint").textContent = next === "single"
      ? "A full 10-K can take a minute or two."
      : "Both periods are analyzed in parallel, then compared.";
    hideAlert();
  };

  $("#tab-single").addEventListener("click", () => setMode("single"));
  $("#tab-compare").addEventListener("click", () => setMode("compare"));

  $$('[role="tab"]').forEach((tab) => tab.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const next = mode === "single" ? "compare" : "single";
    setMode(next);
    $(`#tab-${next}`).focus();
  }));

  // ------------------------------------------------------- inputs & files

  const updateCounter = (field) => {
    const counter = document.querySelector(`[data-counter-for="${field.id}"]`);
    if (counter) counter.textContent = `${field.value.length.toLocaleString()} characters`;
  };

  $$("textarea").forEach((field) => field.addEventListener("input", () => updateCounter(field)));

  const setText = (id, value) => {
    const field = document.getElementById(id);
    field.value = value;
    updateCounter(field);
  };

  const resetDropzone = (inputId) => {
    delete files[inputId];
    const zone = document.querySelector(`[data-drop="${inputId}"]`);
    zone.classList.remove("has-file");
    zone.querySelector(".dropzone__title").textContent = "Or upload a file";
    zone.querySelector(".dropzone__hint").textContent = zone.dataset.defaultHint;
    document.getElementById(inputId).value = "";
  };

  $$("[data-drop]").forEach((zone) => {
    const inputId = zone.dataset.drop;
    const input = document.getElementById(inputId);
    zone.dataset.defaultHint = zone.querySelector(".dropzone__hint").textContent;

    const attach = (file) => {
      if (!file) return;
      if (!/\.(txt|pdf)$/i.test(file.name)) {
        showAlert("Only .txt and .pdf files are supported.");
        return;
      }
      files[inputId] = file;
      zone.classList.add("has-file");
      zone.querySelector(".dropzone__title").textContent = file.name;
      zone.querySelector(".dropzone__hint").textContent =
        `${Math.max(1, Math.round(file.size / 1024)).toLocaleString()} KB · used instead of pasted text`;
      hideAlert();
    };

    zone.addEventListener("click", () => input.click());
    input.addEventListener("change", () => attach(input.files[0]));

    ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("is-over");
    }));
    ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
    }));
    zone.addEventListener("drop", (event) => attach(event.dataTransfer.files[0]));
  });

  // ------------------------------------------------------------- samples

  const fetchSample = async (name) => {
    const response = await fetch(sampleUrl(name));
    if (!response.ok) throw new Error("Sample not found");
    return response.text();
  };

  $$("[data-sample]").forEach((button) => button.addEventListener("click", async () => {
    hideAlert();
    try {
      if (button.dataset.sample === "single") {
        setText("text", await fetchSample("q3"));
        resetDropzone("file");
      } else {
        const [earlier, later] = await Promise.all([fetchSample("q2"), fetchSample("q3")]);
        setText("text_a", earlier);
        setText("text_b", later);
        resetDropzone("file_a");
        resetDropzone("file_b");
      }
    } catch (error) {
      showAlert("The sample transcripts are missing from the samples/ folder.");
    }
  }));

  // --------------------------------------------------------------- EDGAR

  const edgarForms = $$("[data-edgar]");

  const describeFiling = (filing) => `${filing.company} · ${filing.form} filed ${filing.filed}` +
    ` · ${filing.characters.toLocaleString()} characters · ` +
    `<a href="${filing.url}" target="_blank" rel="noopener">view on EDGAR</a>`;

  const fetchFiling = async (ticker, form, index) => {
    const query = new URLSearchParams({ ticker, form, index: String(index) });
    const response = await fetch(`/api/edgar?${query}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "EDGAR request failed.");
    return payload;
  };

  edgarForms.forEach((form) => {
    const status = form.querySelector(".edgar__status");
    const submit = form.querySelector("button[type=submit]");

    const setStatus = (html, isError) => {
      status.innerHTML = html;
      status.classList.toggle("is-error", Boolean(isError));
    };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      hideAlert();

      const ticker = form.querySelector(".edgar__ticker").value.trim();
      const formType = form.querySelector(".edgar__form").value;
      if (!ticker) {
        setStatus("Enter a ticker symbol.", true);
        return;
      }

      const compare = form.dataset.edgar === "compare";
      submit.disabled = true;
      setStatus(compare
        ? `Fetching the last two ${formType} filings…`
        : `Fetching the latest ${formType}…`);

      try {
        if (compare) {
          // index 1 is the earlier filing, so it goes in the left-hand slot.
          const [later, earlier] = await Promise.all([
            fetchFiling(ticker, formType, 0),
            fetchFiling(ticker, formType, 1)
          ]);
          setText("text_a", earlier.text);
          setText("text_b", later.text);
          resetDropzone("file_a");
          resetDropzone("file_b");
          setStatus(`${earlier.company} · ${formType} ${earlier.period} → ${later.period}` +
            ` · ${(earlier.characters + later.characters).toLocaleString()} characters`);
        } else {
          const filing = await fetchFiling(ticker, formType, 0);
          setText("text", filing.text);
          resetDropzone("file");
          setStatus(describeFiling(filing));
        }
      } catch (error) {
        setStatus(escapeHtml(error.message), true);
      } finally {
        submit.disabled = false;
      }
    });
  });

  // ---------------------------------------------------------- alert / busy

  const showAlert = (message) => {
    $("#alert-text").textContent = message;
    $("#alert").classList.add("is-active");
  };

  const hideAlert = () => $("#alert").classList.remove("is-active");

  let elapsedTimer = null;

  const setBusy = (busy) => {
    $("#loading").classList.toggle("is-active", busy);
    $("#run").disabled = busy;
    $("#export").disabled = busy;
    $$("[data-sample], [data-example], [data-edgar] button").forEach((button) => {
      button.disabled = busy;
    });
    clearInterval(elapsedTimer);

    if (busy) {
      $("#progress-log").innerHTML = "";
      const started = Date.now();
      const tick = () => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        $("#elapsed").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      };
      tick();
      elapsedTimer = setInterval(tick, 1000);
    }
  };

  // Progress messages from the two parallel analyses in compare mode arrive
  // interleaved, prefixed "Earlier period: " or "Later period: ". Each prefix
  // is its own lane: a new message only completes earlier steps in its lane,
  // and an unprefixed message completes everything.
  const addProgress = (message) => {
    const match = /^(Earlier period|Later period): (.*)$/.exec(message);
    const lane = match ? match[1] : "";
    const text = match ? match[2] : message;

    $$("#progress-log li.is-current").forEach((item) => {
      if (!lane || item.dataset.lane === lane) {
        item.classList.replace("is-current", "is-done");
      }
    });

    const item = document.createElement("li");
    item.className = lane ? "is-current is-lane" : "is-current";
    item.dataset.lane = lane;
    item.innerHTML = `<span class="progress-log__mark">${icon("check", 12)}</span>
      ${lane ? `<span class="progress-log__lane">${escapeHtml(lane)}</span>` : ""}
      <span>${escapeHtml(text)}</span>`;
    $("#progress-log").appendChild(item);
  };

  // ------------------------------------------------------------ rendering

  const usageLine = (usage) => {
    if (!usage) return "";
    const models = (usage.models || []).map((id) => MODEL_LABELS[id] || id).join(" + ");
    const parts = [
      models,
      `${usage.calls} API ${usage.calls === 1 ? "call" : "calls"}`,
      `${compactNumber(usage.input_tokens)} tokens in`,
      `${compactNumber(usage.output_tokens)} out`,
      `${usage.elapsed_seconds}s`
    ].filter(Boolean).map((part) => `<span>${escapeHtml(part)}</span>`).join("");
    return `<span class="usage"><span class="usage__items">${parts}</span></span>`;
  };

  function renderAnalysis(data) {
    const period = [data.fiscal_period, data.calendar_period]
      .filter((part) => part && part !== "Not stated").join(" · ");
    const meta = [data.document_type, period]
      .filter(Boolean).map((part) => `<span>${escapeHtml(part)}</span>`).join("");

    const docMeta = data._meta || {};
    const chunkNote = docMeta.chunked
      ? `<div class="note-strip">${icon("info", 14)} Long document: condensed from
         ${docMeta.chunks} sections before the final analysis pass.</div>`
      : "";

    return `
      <div class="summary-card">
        <div class="summary-card__head">
          <div>
            <h3>${escapeHtml(data.company_name)}</h3>
            <div class="summary-card__meta">${meta}</div>
          </div>
          <div class="summary-card__read">
            <span class="badge badge--${slug(data.disclosure_read)}">${escapeHtml(data.disclosure_read)}</span>
            <span class="summary-card__read-label">Disclosure read · not a view on the shares</span>
          </div>
        </div>
        <div class="summary-card__note">${prose(data.analyst_summary)}</div>
        <div class="summary-card__reason">${prose(data.disclosure_reason)}</div>
      </div>

      <div class="card-grid">
        ${(data.financial_summary || []).length ? card("revenue", "Reported financials",
          dataTable(["Metric", "This period", "Prior year", "Change"],
            data.financial_summary.map((row) => [row.metric, row.current, row.prior, row.change])),
          true) : ""}
        ${(data.guidance_bridge || []).length ? card("compass", "Guidance bridge",
          dataTable(["Guidance", "Prior", "Current", "Change at midpoint"],
            data.guidance_bridge.map((row) => [row.metric, row.prior, row.current, row.change])),
          true) : ""}
        ${card("building", "Company overview", `<p>${prose(data.company_overview)}</p>`)}
        ${card("revenue", "What the numbers say", `<p>${prose(data.revenue_performance)}</p>`)}
        ${card("growth", "Year-over-year", `<p>${prose(data.yoy_growth)}</p>`)}
        ${card("compass", "Guidance & outlook", `<p>${prose(data.management_guidance)}</p>`)}
        ${card("risk", "Top risks", rankedList(data.top_risks))}
        ${card("opportunity", "Top opportunities", rankedList(data.top_opportunities))}
        ${card("quote", "Key executive quotes",
          (data.key_quotes || []).map(quoteBlock).join(""), true)}
      </div>
      ${chunkNote}`;
  }

  function renderComparison(payload) {
    const { earlier, later, comparison } = payload;
    const earlierLabel = earlier.fiscal_period || "Earlier";
    const laterLabel = later.fiscal_period || "Later";
    const arrows = { up: "▲", down: "▼" };

    const rows = (comparison.deltas || []).map((delta) => `
      <tr>
        <td data-label="Metric">${escapeHtml(delta.metric)}</td>
        <td data-label="${escapeHtml(earlierLabel)}">${escapeHtml(delta.earlier)}</td>
        <td data-label="${escapeHtml(laterLabel)}">${escapeHtml(delta.later)}
          <span class="delta-note">${escapeHtml(delta.commentary)}</span></td>
        <td data-label="Direction">
          <span class="trend trend--${slug(delta.direction)}">
            ${arrows[delta.direction] || "–"} ${escapeHtml(delta.direction)}
          </span>
        </td>
      </tr>`).join("");

    return `
      <div class="summary-card">
        <div class="summary-card__head">
          <div>
            <h3>${escapeHtml(later.company_name || earlier.company_name)}</h3>
            <div class="summary-card__meta">
              <span>${escapeHtml(earlierLabel)} → ${escapeHtml(laterLabel)}</span>
              <span>${escapeHtml(earlier.disclosure_read)} → ${escapeHtml(later.disclosure_read)}</span>
            </div>
          </div>
          <span class="badge badge--${slug(comparison.trajectory)}">${escapeHtml(comparison.trajectory)}</span>
        </div>
        <div class="summary-card__note">${escapeHtml(comparison.headline)}</div>
        <div class="summary-card__reason">${prose(comparison.disclosure_shift)}</div>
      </div>

      <div class="card-grid">
        ${card("scale", "What changed", `
          <div class="table-scroll">
            <table class="delta-table">
              <thead><tr>
                <th>Metric</th><th>${escapeHtml(earlierLabel)}</th>
                <th>${escapeHtml(laterLabel)}</th><th>Direction</th>
              </tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>`, true)}
        ${card("compass", "Guidance change", `<p>${escapeHtml(comparison.guidance_change)}</p>`)}
        ${card("quote", "Tone of management", `<p>${escapeHtml(comparison.tone_change)}</p>`)}
        ${card("risk", "Risk profile", `
          <div class="side-by-side">
            <div class="mini-card">
              <h4>New this period</h4>
              ${plainList(comparison.new_risks, "No new risks introduced.")}
            </div>
            <div class="mini-card">
              <h4>No longer emphasized</h4>
              ${plainList(comparison.resolved_risks, "Nothing dropped from the prior period.")}
            </div>
          </div>`, true)}
        ${card("eye", "What to watch next quarter", rankedList(comparison.what_to_watch), true)}
      </div>`;
  }

  function refreshViews() {
    $("#viewswitch").hidden = !(analysisResult && memoResult);
    if ($("#memo")) $("#memo").hidden = Boolean(memoResult);
  }

  function renderMemo(memo) {
    const figures = (memo.key_figures || []).map((figure) => `
      <div class="memo__figure">
        <div class="memo__figure-label">${escapeHtml(figure.label)}
          ${figure.period ? `<span>${escapeHtml(figure.period)}</span>` : ""}</div>
        <div class="memo__figure-value">${escapeHtml(figure.value)}</div>
        <div class="memo__figure-change">${escapeHtml(figure.change || "")}</div>
      </div>`).join("");

    const caseColumn = (title, items, kind) => `
      <div class="memo__case memo__case--${kind}">
        <div class="memo__case-title">${escapeHtml(title)}</div>
        <ol class="memo__case-list">${(items || []).map((item) => `
          <li>
            <p class="memo__claim">${prose(item.claim)}</p>
            <p class="memo__evidence">${prose(item.evidence)}</p>
            ${item.implication ? `<p class="memo__implication">${prose(item.implication)}</p>` : ""}
          </li>`).join("")}</ol>
      </div>`;

    const section = (label, body) => `
      <section class="memo__section">
        <h3 class="memo__label">${escapeHtml(label)}</h3>
        <div class="memo__body">${body}</div>
      </section>`;

    return `
      <article class="memo">
        <header class="memo__head">
          <div class="memo__masthead">
            <span>Rival Edge</span>
            <span>Investment memo</span>
          </div>
          <div class="memo__title">
            <h2>${escapeHtml(memo.company_name)}</h2>
            <span class="memo__read memo__read--${slug(memo.disclosure_read)}">
              <i></i>${escapeHtml(memo.disclosure_read)}
            </span>
          </div>
          <p class="memo__basis">${escapeHtml(memo.basis)}</p>
          <p class="memo__read-line">${prose(memo.disclosure_reason)}</p>
        </header>

        <div class="memo__figures">${figures}</div>

        ${section("Situation", `<p class="memo__lede">${prose(memo.situation)}</p>`)}
        ${section("The two cases", `<div class="memo__cases">
            ${caseColumn("Bull", memo.bull_case, "bull")}
            ${caseColumn("Bear", memo.bear_case, "bear")}
          </div>`)}
        ${section("Catalysts", `<ul class="memo__catalysts">${(memo.catalysts || []).map((c) => `
            <li><span>${prose(c.event)}</span><time>${escapeHtml(c.timing)}</time></li>`).join("")}</ul>`)}
        ${section("Still to answer", `<ol class="memo__questions">${(memo.open_questions || []).map((q) => `
            <li>${prose(q)}</li>`).join("")}</ol>`)}
        ${(memo.what_would_change_the_read || []).length ? section("What would change the read",
          `<ol class="memo__questions">${memo.what_would_change_the_read.map((item) => `
            <li>${prose(item)}</li>`).join("")}</ol>`) : ""}
        ${section("In their words", (memo.quotes || []).map((q) => `
            <blockquote class="memo__quote">
              <p>“${prose(q.quote)}”</p>
              <cite>${escapeHtml(q.speaker)}${q.context ? `
                <span class="quote__context quote__context--${slug(q.context)}">${escapeHtml(q.context)}</span>` : ""}</cite>
            </blockquote>`).join(""))}

        <footer class="memo__foot">
          Drafted from company disclosure. No valuation, consensus or peer data. Not investment advice.
        </footer>
      </article>`;
  }

  function showResult(resultMode, data, options) {
    const opts = options || {};
    const usage = (data._meta || {}).usage;

    lastResult = { mode: resultMode, data };
    if (resultMode === "memo") {
      memoResult = { mode: resultMode, data, options: opts };
    } else {
      analysisResult = { mode: resultMode, data, options: opts };
    }
    refreshViews();
    $$(".viewswitch__option").forEach((button) => {
      button.classList.toggle("is-on", button.dataset.view ===
        (resultMode === "memo" ? "memo" : "analysis"));
    });

    $("#results-title").textContent = resultMode === "memo"
      ? "Investment memo"
      : resultMode === "single" ? "Analysis" : "Period comparison";
    const generated = data._meta && data._meta.generated_at
      ? `, generated ${escapeHtml(data._meta.generated_at)}` : "";
    $("#provenance").innerHTML = opts.example
      ? `<span class="pill">Saved example</span><span class="pill-note">Real output from the bundled
         sample transcripts${generated}</span>${usageLine(usage)}`
      : usageLine(usage);
    $("#results-body").innerHTML =
      resultMode === "memo" ? renderMemo(data)
        : resultMode === "single" ? renderAnalysis(data) : renderComparison(data);
    $("#results").classList.add("is-active");
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // -------------------------------------------------------------- submit

  // Reads a newline-delimited JSON stream and hands each event to onEvent.
  async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) onEvent(JSON.parse(line));
      }
      if (done) break;
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
  }

  async function run() {
    hideAlert();
    if (DEMO) {
      showAlert("This static demo can't run live analysis. Use “View example” to see saved output, " +
        "or clone the repository and add an API key to analyze your own documents.");
      return;
    }
    const body = new FormData();

    if (mode === "single") {
      if (!$("#text").value.trim() && !files.file) {
        showAlert("Paste a transcript, upload a file, or load the sample first.");
        return;
      }
      body.append("text", $("#text").value);
      if (files.file) body.append("file", files.file);
    } else {
      const hasEarlier = $("#text_a").value.trim() || files.file_a;
      const hasLater = $("#text_b").value.trim() || files.file_b;
      if (!hasEarlier || !hasLater) {
        showAlert("Both periods are required to run a comparison.");
        return;
      }
      body.append("text_a", $("#text_a").value);
      body.append("text_b", $("#text_b").value);
      if (files.file_a) body.append("file_a", files.file_a);
      if (files.file_b) body.append("file_b", files.file_b);
    }

    const runMode = mode;
    setBusy(true);
    $("#results").classList.remove("is-active");

    try {
      const response = await fetch(runMode === "single" ? "/api/analyze" : "/api/compare", {
        method: "POST",
        body
      });

      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        showAlert(failure.error || "The analysis failed. Try again.");
        return;
      }

      let finished = false;
      await readEvents(response, (event) => {
        if (event.type === "progress") addProgress(event.message);
        if (event.type === "result") {
          finished = true;
          $$("#progress-log li.is-current").forEach((li) => li.classList.replace("is-current", "is-done"));
          memoResult = null;
          showResult(runMode, event.data);
        }
        if (event.type === "error") { finished = true; showAlert(event.error); }
      });
      if (!finished) showAlert("The connection closed before the analysis finished.");
    } catch (error) {
      showAlert("Could not reach the server. Is the Flask app still running?");
    } finally {
      setBusy(false);
    }
  }

  $("#run").addEventListener("click", run);

  // ---------------------------------------------------------------- memo

  const memoButton = $("#memo");

  if (memoButton) {
    memoButton.addEventListener("click", async () => {
      if (!analysisResult) {
        showAlert("Run an analysis first, then draft the memo.");
        return;
      }
      hideAlert();

      const body = new FormData();
      body.append("result", JSON.stringify(analysisResult.data));
      if (analysisResult.mode === "single") {
        body.append("text", $("#text").value);
      } else {
        body.append("text", $("#text_a").value);
        body.append("text_b", $("#text_b").value);
      }

      setBusy(true);
      try {
        const response = await fetch("/api/memo", { method: "POST", body });
        if (!response.ok) {
          const failure = await response.json().catch(() => ({}));
          showAlert(failure.error || "The memo could not be drafted.");
          return;
        }
        let finished = false;
        await readEvents(response, (event) => {
          if (event.type === "progress") addProgress(event.message);
          if (event.type === "result") { finished = true; showResult("memo", event.data); }
          if (event.type === "error") { finished = true; showAlert(event.error); }
        });
        if (!finished) showAlert("The connection closed before the memo was finished.");
      } catch (error) {
        showAlert("Could not reach the server. Is the Flask app still running?");
      } finally {
        setBusy(false);
      }
    });
  }

  $$(".viewswitch__option").forEach((button) => button.addEventListener("click", () => {
    const wanted = button.dataset.view === "memo" ? memoResult : analysisResult;
    if (wanted) showResult(wanted.mode, wanted.data, wanted.options);
  }));

  // ------------------------------------------------------------- examples

  $$("[data-example]").forEach((button) => button.addEventListener("click", async () => {
    hideAlert();
    const kind = button.dataset.example === "memo"
      ? "memo"
      : mode === "single" ? "analysis" : "comparison";
    try {
      const response = await fetch(exampleUrl(kind));
      if (!response.ok) throw new Error("missing");
      const payload = await response.json();
      if (kind !== "memo") memoResult = null;
      showResult(kind === "memo" ? "memo" : mode, payload, { example: true });

      // The saved memo rides along, so the demo can switch to it.
      if (kind !== "memo") {
        const memo = await fetch(exampleUrl("memo")).catch(() => null);
        if (memo && memo.ok) {
          memoResult = { mode: "memo", data: await memo.json(), options: { example: true } };
          refreshViews();
        }
      }
    } catch (error) {
      showAlert("No saved example is available. Run scripts/generate_examples.py to create one.");
    }
  }));

  // -------------------------------------------------------------- export

  $("#export").addEventListener("click", async () => {
    if (!lastResult) return;

    if (DEMO) {
      const link = document.createElement("a");
      const file = { single: "analysis", compare: "comparison", memo: "memo" }[lastResult.mode];
      link.href = `docs/example-${file}.pdf`;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
      return;
    }
    const button = $("#export");
    button.disabled = true;

    try {
      const response = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastResult)
      });
      if (!response.ok) {
        showAlert("The PDF could not be generated.");
        return;
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const nameMatch = /filename="?([^";]+)"?/.exec(disposition);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = nameMatch ? nameMatch[1] : "rival-edge-report.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally {
      button.disabled = false;
    }
  });
})();
