let draft = null;

const $ = (id) => document.getElementById(id);

function todayISO() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function todayLabel() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "long",
    year: "numeric",
  }).formatToParts(new Date());
  const day = parts.find((p) => p.type === "day")?.value;
  const month = parts.find((p) => p.type === "month")?.value;
  const year = parts.find((p) => p.type === "year")?.value;
  return `${day} ${month} ${year}`;
}

function setGenerateButtonLabel(busy = false) {
  const btn = $("btnGenerate");
  btn.textContent = busy
    ? "Generating…"
    : `Generate draft for ${todayLabel()}`;
}

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3500);
}

function bulletsToText(arr) {
  return (arr || []).join("\n");
}

function textToBullets(text) {
  return (text || "")
    .split("\n")
    .map((s) => s.replace(/^[\s•\-\*]+/, "").trim())
    .filter(Boolean)
    .map((s) => s.replace(/[\u2014\u2013]/g, "-"));
}

function directionFromChange(change) {
  if (!change) return "flat";
  if (change.includes("▲") || change.trim().startsWith("+")) return "up";
  if (change.includes("▼") || change.trim().startsWith("-")) return "down";
  return "flat";
}

function renderEditableTable(containerId, rows, columns) {
  const root = $(containerId);
  if (!rows || !rows.length) {
    root.innerHTML = "<p class='hint'>No rows. Add manually after generate if needed.</p>";
    root.datasetcols = JSON.stringify(columns);
    return;
  }
  const head = columns.map((c) => `<th>${c.label}</th>`).join("");
  const body = rows
    .map((row, idx) => {
      const cells = columns
        .map((c) => {
          const val = row[c.key] ?? "";
          return `<td><input data-row="${idx}" data-key="${c.key}" value="${escapeAttr(val)}" /></td>`;
        })
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  root.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function escapeAttr(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function readTable(containerId, columns) {
  const root = $(containerId);
  const inputs = [...root.querySelectorAll("input[data-row]")];
  if (!inputs.length) return [];
  const map = {};
  for (const input of inputs) {
    const r = Number(input.dataset.row);
    const k = input.dataset.key;
    if (!map[r]) map[r] = {};
    map[r][k] = input.value.trim();
  }
  return Object.keys(map)
    .sort((a, b) => Number(a) - Number(b))
    .map((k) => {
      const row = map[k];
      // preserve/derive direction when present in schema
      if ("change" in row && !row.direction) row.direction = directionFromChange(row.change);
      if ("move" in row && !row.direction) row.direction = directionFromChange(row.move);
      return row;
    });
}

function fillForm(d) {
  $("nTakeaways").value = bulletsToText(d.narrative.key_takeaways);

  $("adAdvance").value = d.advances_declines.advance ?? "";
  $("adDecline").value = d.advances_declines.decline ?? "";
  $("adRatio").value = d.advances_declines.ratio ?? "";
  $("adNote").value = d.advances_declines.note || "";

  const newsRows = (d.narrative.news_updates || []).map((item) => {
    if (typeof item === "string") return { text: item, source: "" };
    return { text: item.text || "", source: item.source || "" };
  });
  renderEditableTable("tblNews", newsRows, [
    { key: "text", label: "Update" },
    { key: "source", label: "Source (draft only)" },
  ]);

  renderEditableTable("tblSnapshot", d.market_snapshot, [
    { key: "name", label: "Index" },
    { key: "close", label: "Close" },
    { key: "change", label: "Change" },
  ]);
  renderEditableTable("tblGlobal", d.global_markets, [
    { key: "name", label: "Name" },
    { key: "close", label: "Level" },
    { key: "change", label: "Change" },
  ]);
  renderEditableTable("tblSectors", d.sectors, [
    { key: "name", label: "Sector" },
    { key: "change", label: "Change" },
  ]);
  renderEditableTable("tblGainers", d.gainers, [
    { key: "company", label: "Company" },
    { key: "price", label: "Price" },
    { key: "change", label: "Change" },
  ]);
  renderEditableTable("tblLosers", d.losers, [
    { key: "company", label: "Company" },
    { key: "price", label: "Price" },
    { key: "change", label: "Change" },
  ]);
  renderEditableTable("tblFocus", d.stocks_in_focus, [
    { key: "stock", label: "Stock" },
    { key: "move", label: "Move" },
    { key: "whats_happening", label: "What's Happening" },
  ]);
  renderEditableTable("tblCommodities", d.commodities, [
    { key: "commodity", label: "Commodity" },
    { key: "price", label: "Price" },
    { key: "change", label: "1-Day Change" },
  ]);
}

function collectDraft() {
  if (!draft) return null;
  const adv = $("adAdvance").value;
  const dec = $("adDecline").value;
  const ratio = $("adRatio").value;

  const next = {
    ...draft,
    narrative: {
      key_takeaways: textToBullets($("nTakeaways").value),
      news_updates: readTable("tblNews")
        .filter((r) => (r.text || "").trim())
        .map((r) => ({
          text: (r.text || "").replace(/[\u2014\u2013]/g, "-").trim(),
          source: (r.source || "").trim(),
        })),
    },
    market_snapshot: readTable("tblSnapshot").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    global_markets: readTable("tblGlobal").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    sectors: readTable("tblSectors").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    gainers: readTable("tblGainers").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    losers: readTable("tblLosers").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    stocks_in_focus: readTable("tblFocus").map((r) => ({
      ...r,
      whats_happening: (r.whats_happening || "").replace(/[\u2014\u2013]/g, "-"),
      direction: directionFromChange(r.move),
    })),
    commodities: readTable("tblCommodities").map((r) => ({
      ...r,
      direction: directionFromChange(r.change),
    })),
    advances_declines: {
      advance: adv === "" ? null : Number(adv),
      decline: dec === "" ? null : Number(dec),
      ratio: ratio === "" ? null : Number(ratio),
      note: $("adNote").value.trim(),
    },
  };
  draft = next;
  return draft;
}

function renderPreview(d) {
  const list = (arr) =>
    `<ul>${(arr || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  const table = (headers, rows, cellsFn) => {
    const head = headers.map((h) => `<th>${h}</th>`).join("");
    const body = (rows || [])
      .map((r) => `<tr>${cellsFn(r).map((c) => `<td class="${c.cls || ""}">${escapeHtml(c.text)}</td>`).join("")}</tr>`)
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  };

  $("preview").innerHTML = `
    <h1>Daily Market Update</h1>
    <div class="date">${escapeHtml(d.date_label)}</div>

    <h2>Key Takeaways</h2>
    ${list(d.narrative.key_takeaways)}

    <h2>Market Snapshot</h2>
    ${table(["Index", "Close", "Change"], d.market_snapshot, (r) => [
      { text: r.name }, { text: r.close }, { text: r.change, cls: r.direction },
    ])}

    <h2>Global Markets</h2>
    ${table(["Name", "Level", "Change"], d.global_markets, (r) => [
      { text: r.name }, { text: r.close }, { text: r.change, cls: r.direction },
    ])}

    <h2>Sectors Trending Today</h2>
    ${table(["Sector", "Change"], d.sectors, (r) => [
      { text: r.name }, { text: r.change, cls: r.direction },
    ])}

    <h2>Top Gainers - NIFTY 50</h2>
    ${table(["Company", "Price", "Change"], d.gainers, (r) => [
      { text: r.company }, { text: r.price }, { text: r.change, cls: r.direction },
    ])}

    <h2>Top Losers - NIFTY 50</h2>
    ${table(["Company", "Price", "Change"], d.losers, (r) => [
      { text: r.company }, { text: r.price }, { text: r.change, cls: r.direction },
    ])}

    <h2>Advances / Declines</h2>
    <div class="ad-block">
      <p>Advance - ${d.advances_declines.advance ?? "-"}</p>
      <p>Decline - ${d.advances_declines.decline ?? "-"}</p>
      <p>A/D ratio: ${d.advances_declines.ratio ?? "-"}</p>
      ${d.advances_declines.note ? `<p><em>${escapeHtml(d.advances_declines.note)}</em></p>` : ""}
    </div>

    <h2>News &amp; Updates</h2>
    ${list((d.narrative.news_updates || []).map((item) => (typeof item === "string" ? item : item.text)))}

    <h2>Stocks in Focus</h2>
    ${table(["Stock", "Move", "What's Happening"], d.stocks_in_focus, (r) => [
      { text: r.stock }, { text: r.move, cls: r.direction }, { text: r.whats_happening },
    ])}

    <h2>Commodity Market Watch</h2>
    ${table(["Commodity", "Price", "1-Day Change"], d.commodities, (r) => [
      { text: r.commodity }, { text: r.price }, { text: r.change, cls: r.direction },
    ])}
  `;
}

function padCell(text, width) {
  const s = String(text ?? "");
  if (s.length >= width) return s;
  return s + " ".repeat(width - s.length);
}

function formatTableText(headers, rows) {
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((r) => String(r[i] ?? "").length), 4)
  );
  const line = (cells) => cells.map((c, i) => padCell(c, widths[i])).join("  ");
  const rule = widths.map((w) => "-".repeat(w)).join("  ");
  return [line(headers), rule, ...rows.map((r) => line(r))].join("\n");
}

function formatReportPlainText(d) {
  const bullets = (arr) =>
    (arr || []).map((x) => `• ${x}`).join("\n") || "• -";
  const news = (d.narrative.news_updates || []).map((item) =>
    typeof item === "string" ? item : item.text
  );
  const ad = d.advances_declines || {};
  const sections = [
    "Daily Market Update",
    d.date_label || "",
    "",
    "Key Takeaways",
    bullets(d.narrative.key_takeaways),
    "",
    "Market Snapshot",
    formatTableText(
      ["Index", "Close", "Change"],
      (d.market_snapshot || []).map((r) => [r.name, r.close, r.change])
    ),
    "",
    "Global Markets",
    formatTableText(
      ["Name", "Level", "Change"],
      (d.global_markets || []).map((r) => [r.name, r.close, r.change])
    ),
    "",
    "Sectors Trending Today",
    formatTableText(
      ["Sector", "Change"],
      (d.sectors || []).map((r) => [r.name, r.change])
    ),
    "",
    "Top Gainers - NIFTY 50",
    formatTableText(
      ["Company", "Price", "Change"],
      (d.gainers || []).map((r) => [r.company, r.price, r.change])
    ),
    "",
    "Top Losers - NIFTY 50",
    formatTableText(
      ["Company", "Price", "Change"],
      (d.losers || []).map((r) => [r.company, r.price, r.change])
    ),
    "",
    "Advances / Declines",
    `Advance - ${ad.advance ?? "-"}`,
    `Decline - ${ad.decline ?? "-"}`,
    `A/D ratio: ${ad.ratio ?? "-"}`,
    ad.note ? ad.note : "",
    "",
    "News & Updates",
    bullets(news),
    "",
    "Stocks in Focus",
    formatTableText(
      ["Stock", "Move", "What's Happening"],
      (d.stocks_in_focus || []).map((r) => [r.stock, r.move, r.whats_happening])
    ),
    "",
    "Commodity Market Watch",
    formatTableText(
      ["Commodity", "Price", "1-Day Change"],
      (d.commodities || []).map((r) => [r.commodity, r.price, r.change])
    ),
  ];
  return sections.filter((line, idx, arr) => !(line === "" && arr[idx - 1] === "")).join("\n").trim() + "\n";
}

function setCopyEnabled(enabled) {
  $("btnCopy").disabled = !enabled;
  $("btnCopyPreview").disabled = !enabled;
}

async function copyReport() {
  const current = collectDraft();
  if (!current) {
    showToast("Generate a draft first.");
    return;
  }
  const text = formatReportPlainText(current);
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    showToast("Report copied. Paste into email, WhatsApp, or docs.");
  } catch (e) {
    showToast(`Copy failed: ${e.message}`);
  }
}

function renderStatus(status) {
  const grid = $("statusGrid");
  grid.innerHTML = Object.entries(status)
    .map(([k, v]) => {
      const cls = String(v).startsWith("failed")
        ? "failed"
        : v === "ok"
          ? "ok"
          : v === "skipped"
            ? "skipped"
            : "empty";
      return `<div class="status-chip ${cls}"><strong>${escapeHtml(k.replaceAll("_", " "))}</strong>${escapeHtml(v)}</div>`;
    })
    .join("");
  $("statusPanel").classList.remove("hidden");
}

async function generate() {
  const btn = $("btnGenerate");
  btn.disabled = true;
  setGenerateButtonLabel(true);
  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: todayISO(), include_narrative: true }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    draft = data.draft;
    fillForm(draft);
    renderPreview(draft);
    renderStatus(data.source_status);
    $("workspace").classList.remove("hidden");
    $("btnPdf").disabled = false;
    setCopyEnabled(true);
    showToast("Draft ready. Edit anything, then export PDF.");
  } catch (e) {
    showToast(`Generate failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    setGenerateButtonLabel(false);
  }
}

async function exportPdf() {
  const current = collectDraft();
  if (!current) return;
  $("btnPdf").disabled = true;
  try {
    const res = await fetch("/api/export/pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: current }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `Daily_Market_Update_${current.date_label.replaceAll(" ", "_")}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
    showToast("PDF downloaded.");
  } catch (e) {
    showToast(`PDF export failed: ${e.message}`);
  } finally {
    $("btnPdf").disabled = false;
  }
}

function bindLivePreview() {
  const ids = [
    "nTakeaways",
    "adAdvance", "adDecline", "adRatio", "adNote",
  ];
  for (const id of ids) {
    $(id).addEventListener("input", () => {
      const d = collectDraft();
      if (d) renderPreview(d);
    });
  }
  document.addEventListener("input", (e) => {
    if (e.target.matches(".table-edit input")) {
      const d = collectDraft();
      if (d) renderPreview(d);
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  setGenerateButtonLabel(false);
  $("btnGenerate").addEventListener("click", generate);
  $("btnPdf").addEventListener("click", exportPdf);
  $("btnCopy").addEventListener("click", copyReport);
  $("btnCopyPreview").addEventListener("click", copyReport);
  bindLivePreview();
});
