(() => {
  const API = "/api/v1/pdf";
  let MAX_MB = 50;
  const $ = (id) => document.getElementById(id);
  const form = $("form"), fileInput = $("file"), drop = $("drop");
  const btnCompress = $("btn-compress"), btnAnalyze = $("btn-analyze"), result = $("result");
  let file = null;
  let downloadUrl = null;

  fetch("/api/v1/config").then((r) => r.json()).then((c) => {
    MAX_MB = c.max_upload_mb;
    $("limit-hint").textContent = `Maximal ${MAX_MB} MB`;
    $("docs-link").classList.toggle("hidden", !c.docs_enabled);
  }).catch(() => {});

  // Logo nur anzeigen, wenn app/static/logo.svg vorhanden ist (kein Inline-Handler wegen CSP)
  const logo = $("logo");
  const showLogo = () => logo.classList.remove("hidden");
  if (logo.complete) { if (logo.naturalWidth) showLogo(); else logo.remove(); }
  else { logo.addEventListener("load", showLogo); logo.addEventListener("error", () => logo.remove()); }

  // Letzte Einstellungen merken (nur Komfort, darf fehlen)
  const STORE = "pdf-compress-settings";
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    for (const [name, value] of Object.entries(saved)) {
      if (name === "embed" || name === "sanitize") $(name).checked = !!value;
      else { const el = form.querySelector(`input[name="${name}"][value="${value}"]`); if (el) el.checked = true; }
    }
  } catch {}
  const saveSettings = () => {
    try { localStorage.setItem(STORE, JSON.stringify({ level: val("level"), fallback: val("fallback"), mode: val("mode"), embed: $("embed").checked, sanitize: $("sanitize").checked })); } catch {}
  };

  const val = (name) => form.querySelector(`input[name="${name}"]:checked`).value;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtBytes = (n) => {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace(".", ",")} KB`;
    return `${(n / 1024 / 1024).toFixed(2).replace(".", ",")} MB`;
  };

  function updateFallbackState() {
    $("fallback-wrap").classList.toggle("disabled", !$("embed").checked);
  }
  $("embed").addEventListener("change", () => { updateFallbackState(); saveSettings(); });
  form.addEventListener("change", saveSettings);
  updateFallbackState();

  function setFile(f) {
    if (f && !(f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"))) {
      showError("Bitte eine PDF-Datei auswählen.");
      return;
    }
    if (f && f.size > MAX_MB * 1024 * 1024) {
      showError(`Die Datei ist größer als ${MAX_MB} MB.`);
      return;
    }
    file = f || null;
    $("chip").classList.toggle("show", !!file);
    drop.style.display = file ? "none" : "";
    if (file) { $("chip-name").textContent = file.name; $("chip-size").textContent = fmtBytes(file.size); }
    btnCompress.disabled = btnAnalyze.disabled = !file;
    if (!file) fileInput.value = "";
  }

  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
  $("chip-remove").addEventListener("click", () => { setFile(null); clearResult(); });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
  // Drop auf die ganze Seite erlauben, ohne dass der Browser die PDF öffnet
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });

  function busy(btn, on) {
    btn.classList.toggle("loading", on);
    btnCompress.disabled = btnAnalyze.disabled = on || !file;
    form.querySelectorAll("input").forEach((i) => (i.disabled = on));
  }

  function clearResult() {
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    downloadUrl = null;
    result.classList.remove("show");
    result.innerHTML = "";
  }

  function showError(msg) {
    clearResult();
    result.innerHTML = `<div class="alert err m-0">${esc(msg)}</div>`;
    result.classList.add("show");
  }

  async function errorMessage(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join(", ");
    } catch {}
    return `Fehler ${res.status}`;
  }

  function formData(withParams) {
    const fd = new FormData();
    fd.append("file", file);
    if (withParams) {
      fd.append("level", val("level"));
      fd.append("embed_fonts", $("embed").checked ? "true" : "false");
      fd.append("fallback_font", val("fallback"));
      fd.append("remove_active_content", $("sanitize").checked ? "true" : "false");
    }
    return fd;
  }

  const RESULT_TEXT = {
    compressed: ["ok", "Erfolgreich komprimiert."],
    fonts_only: ["warn", "Schriften eingebettet. Eine Verkleinerung war nicht möglich, deshalb sind die Bilder unverändert."],
    sanitized: ["warn", "Aktive Inhalte entfernt. Eine Verkleinerung war nicht möglich, deshalb sind die Bilder unverändert."],
    original: ["warn", "Keine Verbesserung möglich, das Original wird unverändert zurückgegeben."],
  };
  const FONT_STATUS = {
    already_embedded: ["ok", "bereits eingebettet"],
    embedded: ["ok", "eingebettet"],
    fallback: ["info", "Ersatzschrift"],
    skipped: ["warn", "nicht eingebettet"],
  };

  function statsHtml(orig, res) {
    const saved = orig > 0 ? Math.max(0, (1 - res / orig) * 100) : 0;
    return `
      <div class="stats">
        <div class="stat"><div class="label">Original</div><div class="value">${fmtBytes(orig)}</div></div>
        <div class="stat"><div class="label">Ergebnis</div><div class="value">${fmtBytes(res)}</div></div>
        <div class="stat"><div class="label">Ersparnis</div><div class="value">${saved.toFixed(1).replace(".", ",")} %</div></div>
        <div class="stat"><div class="label">Faktor</div><div class="value">${res && res < orig ? `${(orig / res).toFixed(1).replace(".", ",")}×` : "–"}</div></div>
      </div>
      <div class="bar" title="Ergebnis im Verhältnis zum Original"><div data-width="${orig ? Math.min(100, (res / orig) * 100) : 100}"></div></div>`;
  }

  function fontsTable(fonts) {
    if (!fonts || !fonts.length) return `<p class="muted">Keine Schriften gefunden.</p>`;
    const rows = fonts.map((f) => {
      const [cls, text] = FONT_STATUS[f.status] || ["muted", f.status];
      return `<tr><td>${esc(f.name)}</td><td class="muted">${esc(f.subtype)}</td>
        <td><span class="badge ${cls}">${text}</span></td>
        <td>${f.used_font ? esc(f.used_font) : '<span class="muted">–</span>'}${f.reason ? `<div class="muted fs-12">${esc(f.reason)}</div>` : ""}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table><thead><tr><th>Schrift</th><th>Typ</th><th>Status</th><th>Verwendet</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function activeHtml(items, title) {
    return items && items.length ? `<h2>${title}</h2><ul class="removed">${items.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  }

  // Balkenbreite per CSSOM setzen (Inline-style-Attribute sind durch die CSP verboten)
  function applyBars() {
    result.querySelectorAll("[data-width]").forEach((el) => { el.style.width = `${el.dataset.width}%`; });
  }

  function warningsHtml(warnings) {
    return warnings && warnings.length ? `<h2>Hinweise</h2><ul class="warnings">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  }

  function downloadButton(name) {
    return `<a class="btn btn-primary" href="${downloadUrl}" download="${esc(name)}">PDF herunterladen</a>`;
  }

  function filenameFromHeader(header, fallback) {
    if (!header) return fallback;
    const star = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) { try { return decodeURIComponent(star[1]); } catch {} }
    const plain = header.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : fallback;
  }

  async function compress() {
    busy(btnCompress, true);
    clearResult();
    const mode = val("mode");
    try {
      const res = await fetch(`${API}/${mode === "base64" ? "compress/base64" : "compress"}`, { method: "POST", body: formData(true) });
      if (!res.ok) return showError(await errorMessage(res));

      if (mode === "file") {
        const h = (k) => res.headers.get(k);
        const blob = await res.blob();
        downloadUrl = URL.createObjectURL(blob);
        const name = filenameFromHeader(h("content-disposition"), "document_compressed.pdf");
        const [cls, text] = RESULT_TEXT[h("x-result")] || ["warn", h("x-result")];
        const warnings = +h("x-warnings") || 0;
        result.innerHTML = `
          <div class="alert ${cls}">${text}</div>
          ${statsHtml(+h("x-original-size"), +h("x-result-size"))}
          <div class="table-wrap"><table><tbody>
            <tr><td class="muted">Stufe</td><td>${esc(h("x-compression-level"))}</td></tr>
            <tr><td class="muted">Verkleinerte Bilder</td><td>${esc(h("x-images-downsampled"))}</td></tr>
            <tr><td class="muted">Eingebettete Schriften</td><td>${esc(h("x-fonts-embedded"))} Original, ${esc(h("x-fonts-fallback"))} Ersatz</td></tr>
            <tr><td class="muted">Entfernte aktive Inhalte</td><td>${esc(h("x-active-content-removed") || "0")}</td></tr>
            <tr><td class="muted">Hinweise</td><td>${warnings}${warnings ? ' <span class="muted">(Details im Base64-Modus)</span>' : ""}</td></tr>
          </tbody></table></div>
          <div class="actions mt-0">${downloadButton(name)}</div>`;
      } else {
        const body = await res.json();
        const r = body.report;
        const bin = Uint8Array.from(atob(body.content_base64), (c) => c.charCodeAt(0));
        downloadUrl = URL.createObjectURL(new Blob([bin], { type: "application/pdf" }));
        const [cls, text] = RESULT_TEXT[r.result] || ["warn", r.result];
        result.innerHTML = `
          <div class="alert ${cls}">${text}</div>
          ${statsHtml(r.original_size, r.result_size)}
          <p class="muted mt-0">Stufe <strong>${esc(r.level)}</strong> (${r.dpi} DPI) · ${r.images_downsampled} Bild(er) verkleinert</p>
          <h2>Schriften</h2>${fontsTable(r.fonts)}
          ${activeHtml(r.active_content_removed, "Entfernte aktive Inhalte")}
          ${warningsHtml(r.warnings)}
          <div class="row-between"><h2 class="m-0">Base64</h2><span class="muted fs-13">${fmtBytes(body.content_base64.length)}</span></div>
          <textarea id="b64" readonly>${esc(body.content_base64)}</textarea>
          <div class="actions">
            ${downloadButton(body.filename)}
            <button type="button" class="btn btn-secondary" id="copy-b64">Base64 kopieren</button>
            <button type="button" class="btn btn-secondary" id="copy-json">JSON kopieren</button>
          </div>`;
        const copy = (btn, text) => navigator.clipboard.writeText(text).then(() => {
          const old = btn.textContent; btn.textContent = "Kopiert ✓"; setTimeout(() => (btn.textContent = old), 1500);
        }).catch(() => { $("b64").select(); });
        $("copy-b64").addEventListener("click", (e) => copy(e.currentTarget, body.content_base64));
        $("copy-json").addEventListener("click", (e) => copy(e.currentTarget, JSON.stringify(body, null, 2)));
      }
      applyBars();
      result.classList.add("show");
      result.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      showError("Server nicht erreichbar.");
    } finally {
      busy(btnCompress, false);
    }
  }

  async function analyze() {
    busy(btnAnalyze, true);
    clearResult();
    try {
      const res = await fetch(`${API}/analyze`, { method: "POST", body: formData(false) });
      if (!res.ok) return showError(await errorMessage(res));
      const a = await res.json();
      const missing = a.fonts.filter((f) => f.status !== "already_embedded").length;
      const active = a.active_content || [];
      const images = a.images.length
        ? `<div class="table-wrap"><table><thead><tr><th>Pixel</th><th>Effektive DPI</th><th>Format</th><th>Hinweis</th></tr></thead><tbody>${a.images.map((i) => `
            <tr><td>${i.width} × ${i.height}</td><td>${i.effective_dpi ?? '<span class="muted">unbekannt</span>'}</td>
            <td class="muted">${esc(i.filter || "–")}</td><td>${i.skip_reason ? `<span class="badge muted">${esc(i.skip_reason)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">Keine Bilder gefunden.</p>`;
      result.innerHTML = `
        <div class="alert ${missing ? "warn" : "ok"}">${missing ? `${missing} Schrift(en) nicht eingebettet.` : "Alle Schriften sind eingebettet."}</div>
        ${active.length ? `<div class="alert warn">${active.length} aktive(r) Inhalt(e) gefunden. Beim Komprimieren werden sie standardmäßig entfernt.</div>` : ""}
        <p class="muted mt-0">${esc(a.filename)} · ${fmtBytes(a.size)} · ${a.pages} Seite(n)</p>
        <h2>Schriften</h2>${fontsTable(a.fonts)}
        <h2>Bilder</h2>${images}
        ${activeHtml(active, "Aktive Inhalte")}`;
      result.classList.add("show");
      result.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      showError("Server nicht erreichbar.");
    } finally {
      busy(btnAnalyze, false);
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); if (file) compress(); });
  btnAnalyze.addEventListener("click", () => file && analyze());
})();
