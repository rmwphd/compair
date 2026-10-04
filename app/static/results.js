// Results dashboard: fetches /admin/api/surveys/{id}/results and draws tables + SVG charts.
// All user-provided strings (item labels, group names) go through textContent.
(() => {
  const form = document.getElementById("controls");
  const surveyId = form.dataset.survey;
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  let data = null;
  let showAll = false;
  let inflight = null;

  // ------------------------------------------------------------------ helpers
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const el = (tag, attrs = {}, parent, text) => {
    const isSvg = ["svg", "g", "line", "circle", "path", "rect", "text", "title"].includes(tag);
    const node = isSvg ? document.createElementNS(NS, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  };
  const fmt = (v, d = 2) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + v.toFixed(d));
  const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(100 * v)}%`);
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };

  function hexToRgb(h) {
    h = h.replace("#", "");
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }
  const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  function diverging(v, maxAbs) {
    const t = Math.max(-1, Math.min(1, maxAbs ? v / maxAbs : 0));
    const mid = hexToRgb(css("--div-mid"));
    const pole = hexToRgb(css(t >= 0 ? "--div-pos" : "--div-neg"));
    return mix(mid, pole, Math.abs(t));
  }
  const rgb = (c) => `rgb(${c.join(",")})`;
  const inkOn = (c) => {
    const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.35 ? "#0b0b0b" : "#ffffff";
  };

  // ------------------------------------------------------------------ tooltip
  const tip = $("tip");
  function showTip(evt, title, rows) {
    clear(tip);
    el("strong", {}, tip, title);
    for (const [key, value, color] of rows) {
      const line = el("div", {}, tip);
      if (color) el("span", { class: "tk", style: `background:${color}` }, line);
      el("strong", {}, line, value);
      line.appendChild(document.createTextNode(`  ${key}`));
    }
    tip.hidden = false;
    const x = (evt.clientX ?? evt.target.getBoundingClientRect().right) + 14;
    const y = (evt.clientY ?? evt.target.getBoundingClientRect().top) + 14;
    tip.style.left = `${Math.min(x, window.innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${Math.min(y, window.innerHeight - tip.offsetHeight - 8)}px`;
  }
  const hideTip = () => { tip.hidden = true; };
  function hoverable(node, title, rowsFn) {
    node.setAttribute("tabindex", "0");
    const show = (e) => showTip(e, title, rowsFn());
    node.addEventListener("pointermove", show);
    node.addEventListener("focus", show);
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("blur", hideTip);
  }

  // ------------------------------------------------------------------ fetch
  function params() {
    const p = new URLSearchParams();
    for (const id of ["g1", "g2"]) if ($(id).value) p.append("group", $(id).value);
    if ($("flt").value) p.append("filter", $("flt").value);
    p.set("min", $("min").value || "10");
    p.set("boot", $("boot").value);
    p.set("ties", $("ties").value);
    p.set("exclude_flagged", $("exf").checked ? "1" : "0");
    return p;
  }

  async function load() {
    if (inflight) inflight.abort();
    inflight = new AbortController();
    $("out").classList.add("loading");
    $("status").textContent = data ? "Updating…" : "Fitting models…";
    $("status").hidden = false;
    try {
      const r = await fetch(`/admin/api/surveys/${surveyId}/results?${params()}`, { signal: inflight.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
      render();
    } catch (e) {
      if (e.name !== "AbortError") $("status").textContent = `Could not load results: ${e.message}`;
      return;
    }
    $("out").classList.remove("loading");
  }

  // ------------------------------------------------------------------ render
  function render() {
    const o = data.overall;
    if (!o) {
      $("status").textContent = "No answered comparisons yet for this selection.";
      $("overall").hidden = $("grouped").hidden = true;
      return;
    }
    $("status").hidden = true;
    $("overall").hidden = false;
    renderTiles(o);
    renderRankTable(o);
    const g = data.groups;
    $("grouped").hidden = !g;
    if (g) {
      renderGroupSummary(g);
      renderHeat(o, g);
      renderBump(o, g);
      setupScatter(g);
      renderTau(g);
    }
  }

  function renderTiles(o) {
    const tiles = clear($("tiles"));
    const add = (v, l) => { const d = el("div", { class: "stat" }, tiles); el("div", { class: "v" }, d, v); el("div", { class: "l" }, d, l); };
    add(o.n_participants.toLocaleString(), "participants");
    add(o.n_comparisons.toLocaleString(), "answered comparisons");
    add(o.items.length.toLocaleString(), "items ranked");
    add(o.nu ? o.nu.toFixed(2) : "–", "tie parameter ν");
    $("overall-note").textContent = data.n_unranked ? `${data.n_unranked} item(s) with no answers yet are not ranked.` : "";
  }

  function ciBar(item, lo, hi) {
    const W = 200, H = 20, pad = 6;
    const x = (v) => pad + ((v - lo) / (hi - lo || 1)) * (W - 2 * pad);
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `score ${fmt(item.theta)}${item.theta_lo !== undefined ? `, 95% interval ${fmt(item.theta_lo)} to ${fmt(item.theta_hi)}` : ""}` });
    el("line", { x1: x(0), x2: x(0), y1: 2, y2: H - 2, class: "baseline" }, svg);
    if (item.theta_lo !== undefined && item.theta_lo !== null)
      el("line", { x1: x(item.theta_lo), x2: x(item.theta_hi), y1: H / 2, y2: H / 2, stroke: css("--s1"),
        "stroke-width": 2, "stroke-linecap": "round", opacity: 0.55 }, svg);
    el("circle", { cx: x(item.theta), cy: H / 2, r: 4, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2 }, svg);
    return svg;
  }

  function renderRankTable(o) {
    const t = clear($("rank-table"));
    const head = el("tr", {}, el("thead", {}, t));
    ["Rank", "Item", "Score", "95% interval", "Win %", "Answers"].forEach((h, i) =>
      el("th", { class: [0, 2, 4, 5].includes(i) ? "num" : "" }, head, h));
    const lo = Math.min(...o.items.map((d) => d.theta_lo ?? d.theta), 0);
    const hi = Math.max(...o.items.map((d) => d.theta_hi ?? d.theta), 0);
    const body = el("tbody", {}, t);
    const rows = showAll ? o.items : o.items.slice(0, 50);
    for (const d of rows) {
      const tr = el("tr", {}, body);
      const rk = d.rank_lo !== undefined && d.rank_lo !== null && (d.rank_lo !== d.rank || d.rank_hi !== d.rank)
        ? `${d.rank}  (${d.rank_lo}–${d.rank_hi})` : `${d.rank}`;
      el("td", { class: "num" }, tr, rk);
      el("td", {}, tr, d.label);
      el("td", { class: "num" }, tr, fmt(d.theta));
      el("td", { class: "ci-cell" }, tr).appendChild(ciBar(d, lo, hi));
      el("td", { class: "num" }, tr, pct(d.win_pct));
      el("td", { class: "num" }, tr, (d.n ?? 0).toLocaleString());
    }
    $("more").hidden = o.items.length <= 50;
    $("more").textContent = showAll ? "Show top 50" : `Show all ${o.items.length}`;
  }

  function groupLabels(g) { return Object.keys(g.results); }

  function renderGroupSummary(g) {
    const lrt = g.lrt;
    const p = $("lrt");
    clear(p);
    if (!lrt) {
      p.textContent = "Need at least two groups with enough participants to compare.";
    } else {
      const pv = lrt.p_value < 0.001 ? "p < 0.001" : `p = ${lrt.p_value.toFixed(3)}`;
      const strong = el("strong", {}, p, lrt.p_value < 0.05 ? "Yes, the evidence says rankings differ between groups. " : "No clear evidence that rankings differ between groups. ");
      strong.style.color = "var(--ink)";
      p.appendChild(document.createTextNode(
        `Likelihood-ratio test of one shared ranking vs. a separate ranking per group: χ² = ${lrt.statistic.toFixed(1)}, df = ${lrt.df}, ${pv}. ` +
        "Small groups and sparse data make this approximate."));
    }
    const parts = Object.entries(g.sizes).map(([k, n]) =>
      `${k}: ${n}${g.insufficient.includes(k) ? ` (below ${data.min_participants}, not ranked)` : ""}`);
    $("sizes").textContent = `Participants per group: ${parts.join(" · ")}`;
  }

  function renderHeat(o, g) {
    const labels = groupLabels(g);
    const t = clear($("heat"));
    const head = el("tr", {}, el("thead", {}, t));
    el("th", {}, head, "Item");
    el("th", { class: "num" }, head, "Overall");
    for (const l of labels) el("th", { class: "num" }, head, `${l} (n=${g.results[l].n_participants})`);
    const byGroup = Object.fromEntries(labels.map((l) => [l, Object.fromEntries(g.results[l].items.map((d) => [d.item_id, d]))]));
    const maxAbs = Math.max(...labels.flatMap((l) => g.results[l].items.map((d) => Math.abs(d.theta))), 0.5);
    const body = el("tbody", {}, t);
    for (const d of (showAll ? o.items : o.items.slice(0, 50))) {
      const tr = el("tr", {}, body);
      el("td", {}, tr, d.label);
      el("td", { class: "num" }, tr, d.rank);
      for (const l of labels) {
        const gd = byGroup[l][d.item_id];
        if (!gd) { el("td", { class: "cell na" }, tr, "–"); continue; }
        const c = diverging(gd.theta, maxAbs);
        const shift = d.rank - gd.rank;
        const td = el("td", { class: "cell", style: `background:${rgb(c)};color:${inkOn(c)}` }, tr,
          `${gd.rank}${shift > 0 ? " ▲" : shift < 0 ? " ▼" : ""}`);
        hoverable(td, d.label, () => [
          [`rank in ${l}`, `#${gd.rank}`],
          ["overall rank", `#${d.rank}`],
          ["score", fmt(gd.theta)],
          ["answers", String(gd.n ?? 0)],
        ]);
      }
    }
  }

  // ---------------------------------------------------------------- bump chart
  function renderBump(o, g) {
    const labels = groupLabels(g);
    const box = clear($("bump"));
    const legend = clear($("bump-legend"));
    if (labels.length < 2) { el("p", { class: "muted" }, box, "Needs at least two groups with enough participants."); return; }
    const ranksBy = Object.fromEntries(labels.map((l) => [l, Object.fromEntries(g.results[l].items.map((d) => [d.item_id, d.theta]))]));
    const mode = $("bump-n").value;
    let items = o.items.filter((d) => labels.every((l) => ranksBy[l][d.item_id] !== undefined));
    // Re-rank within each group among the items in the candidate pool.
    const rerank = (pool) => {
      const out = {};
      for (const l of labels) {
        const sorted = [...pool].sort((a, b) => ranksBy[l][b.item_id] - ranksBy[l][a.item_id]);
        sorted.forEach((d, i) => { (out[d.item_id] ||= {})[l] = i + 1; });
      }
      return out;
    };
    if (mode === "movers") {
      const rr = rerank(items);
      const range = (d) => Math.max(...labels.map((l) => rr[d.item_id][l])) - Math.min(...labels.map((l) => rr[d.item_id][l]));
      items = [...items].sort((a, b) => range(b) - range(a)).slice(0, 20);
    } else if (mode !== "all") {
      items = items.slice(0, Number(mode));
    }
    const rr = rerank(items);
    const spread = (d) => Math.max(...labels.map((l) => rr[d.item_id][l])) - Math.min(...labels.map((l) => rr[d.item_id][l]));
    const highlight = [...items].sort((a, b) => spread(b) - spread(a)).filter((d) => spread(d) > 0).slice(0, 3);
    const hlColor = new Map(highlight.map((d, i) => [d.item_id, css(`--s${i + 1}`)]));

    // legend: highlighted items + "other"
    for (const d of highlight) {
      const span = el("span", {}, legend);
      el("i", { style: `background:${hlColor.get(d.item_id)}` }, span);
      span.appendChild(document.createTextNode(`${trunc(d.label, 40)} (moves ${spread(d)})`));
    }
    const other = el("span", {}, legend);
    el("i", { style: `background:${css("--axis")}` }, other);
    other.appendChild(document.createTextNode("other items · hover any line"));

    const n = items.length;
    const showLabels = n <= 40;
    const labelW = showLabels ? 170 : 30;
    const rowH = n > 40 ? 12 : 24;
    const colW = Math.max(110, Math.min(220, (box.clientWidth - 2 * labelW) / Math.max(labels.length - 1, 1)));
    const W = 2 * labelW + colW * (labels.length - 1);
    const top = 36, H = top + rowH * n + 10;
    const x = (i) => labelW + i * colW;
    const y = (r) => top + (r - 0.5) * rowH;
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `Rank of ${n} items across ${labels.length} groups` }, box);

    labels.forEach((l, i) => {
      el("line", { x1: x(i), x2: x(i), y1: top - 6, y2: H - 6, class: "gridline" }, svg);
      el("text", { x: x(i), y: 14, "text-anchor": "middle", style: "font-weight:600;fill:var(--ink)" }, svg, trunc(l, 24));
      el("text", { x: x(i), y: 28, "text-anchor": "middle", class: "axis-label" }, svg, `n=${g.results[l].n_participants}`);
    });

    const ordered = [...items].sort((a, b) => hlColor.has(a.item_id) - hlColor.has(b.item_id)); // highlighted drawn last
    const groups = [];
    for (const d of ordered) {
      const isHl = hlColor.has(d.item_id);
      const color = isHl ? hlColor.get(d.item_id) : css("--axis");
      const gEl = el("g", { class: "bump-line" }, svg);
      const pts = labels.map((l, i) => [x(i), y(rr[d.item_id][l])]);
      const path = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join("");
      el("path", { d: path, fill: "none", stroke: color, "stroke-width": isHl ? 2.5 : 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, gEl);
      for (const [px, py] of pts) el("circle", { cx: px, cy: py, r: rowH > 12 ? 4 : 2.5, fill: color, stroke: css("--surface"), "stroke-width": 2 }, gEl);
      // hit target wider than the line
      el("path", { d: path, fill: "none", stroke: "transparent", "stroke-width": Math.max(rowH * 0.8, 10) }, gEl);
      if (showLabels) {
        const ls = { "font-weight": isHl ? 650 : 400, style: isHl ? "fill:var(--ink)" : "" };
        el("text", { x: x(0) - 10, y: pts[0][1] + 4, "text-anchor": "end", ...ls }, gEl, trunc(d.label, 24));
        el("text", { x: x(labels.length - 1) + 10, y: pts[pts.length - 1][1] + 4, ...ls }, gEl, trunc(d.label, 24));
      }
      gEl._base = { color, width: isHl ? 2.5 : 1.5 };
      groups.push(gEl);
      hoverable(gEl, d.label, () => labels.map((l) => [l, `#${rr[d.item_id][l]}`]));
      const emphasize = (on) => {
        for (const other of groups) other.style.opacity = on && other !== gEl ? 0.25 : 1;
        const line = gEl.querySelector("path");
        line.setAttribute("stroke", on ? css("--ink") : gEl._base.color);
        line.setAttribute("stroke-width", on ? 3 : gEl._base.width);
        if (on) svg.appendChild(gEl);
      };
      gEl.addEventListener("pointerenter", () => emphasize(true));
      gEl.addEventListener("focus", () => emphasize(true));
      gEl.addEventListener("pointerleave", () => emphasize(false));
      gEl.addEventListener("blur", () => emphasize(false));
    }
    el("p", { class: "muted small" }, box,
      "Ranks are among the items shown, so shifts are relative to this set. Items missing from any group are left out.");
  }

  // ---------------------------------------------------------------- scatter
  function setupScatter(g) {
    const labels = groupLabels(g);
    for (const [id, idx] of [["sx", 0], ["sy", 1]]) {
      const sel = $(id);
      const prev = sel.value;
      clear(sel);
      labels.forEach((l) => el("option", { value: l }, sel, l));
      sel.value = labels.includes(prev) ? prev : labels[Math.min(idx, labels.length - 1)] ?? "";
    }
    renderScatter(g);
  }

  function renderScatter(g) {
    const box = clear($("scatter"));
    const a = $("sx").value, b = $("sy").value;
    const labels = groupLabels(g);
    if (labels.length < 2 || !a || !b) { $("scatter-note").textContent = "Needs at least two groups."; return; }
    const A = Object.fromEntries(g.results[a].items.map((d) => [d.item_id, d]));
    const B = Object.fromEntries(g.results[b].items.map((d) => [d.item_id, d]));
    const pts = Object.keys(A).filter((k) => B[k]).map((k) => ({ a: A[k], b: B[k] }));
    const tau = g.tau && g.tau.labels.includes(a) && g.tau.labels.includes(b)
      ? g.tau.matrix[g.tau.labels.indexOf(a)][g.tau.labels.indexOf(b)] : null;
    $("scatter-note").textContent =
      `Each dot is an item. On the diagonal = same score in both groups; above it = ${b} likes it more; below = ${a} likes it more.` +
      (tau !== null ? ` Kendall τ = ${tau.toFixed(2)}.` : "");

    const ext = Math.max(1, ...pts.flatMap((p) => [Math.abs(p.a.theta), Math.abs(p.b.theta)])) * 1.1;
    const size = Math.min(560, Math.max(320, box.clientWidth));
    const m = { l: 48, r: 16, t: 12, b: 40 };
    const S = size - m.l - m.r;
    const sx = (v) => m.l + ((v + ext) / (2 * ext)) * S;
    const sy = (v) => m.t + S - ((v + ext) / (2 * ext)) * S;
    const svg = el("svg", { width: size, height: S + m.t + m.b, role: "img",
      "aria-label": `Item scores in ${a} versus ${b}` }, box);
    const step = ext > 3 ? 1 : 0.5;
    for (let v = -Math.floor(ext / step) * step; v <= ext; v += step) {
      const cls = Math.abs(v) < 1e-9 ? "baseline" : "gridline";
      el("line", { x1: sx(v), x2: sx(v), y1: m.t, y2: m.t + S, class: cls }, svg);
      el("line", { x1: m.l, x2: m.l + S, y1: sy(v), y2: sy(v), class: cls }, svg);
      el("text", { x: sx(v), y: m.t + S + 14, "text-anchor": "middle", class: "axis-label" }, svg, String(+v.toFixed(1)));
      el("text", { x: m.l - 6, y: sy(v) + 4, "text-anchor": "end", class: "axis-label" }, svg, String(+v.toFixed(1)));
    }
    el("line", { x1: sx(-ext), y1: sy(-ext), x2: sx(ext), y2: sy(ext), stroke: css("--muted"), "stroke-width": 1 }, svg);
    el("text", { x: m.l + S / 2, y: m.t + S + 32, "text-anchor": "middle" }, svg, `score in ${trunc(a, 40)}`);
    el("text", { x: 12, y: m.t + S / 2, "text-anchor": "middle", transform: `rotate(-90 12 ${m.t + S / 2})` }, svg, `score in ${trunc(b, 40)}`);

    const byDiff = [...pts].sort((p, q) => Math.abs(q.b.theta - q.a.theta) - Math.abs(p.b.theta - p.a.theta));
    const labelled = new Set(byDiff.slice(0, 5).map((p) => p.a.item_id));
    for (const p of pts) {
      const cx = sx(p.a.theta), cy = sy(p.b.theta);
      const gEl = el("g", {}, svg);
      el("circle", { cx, cy, r: 12, fill: "transparent" }, gEl);
      el("circle", { cx, cy, r: 4.5, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2 }, gEl);
      if (labelled.has(p.a.item_id))
        el("text", { x: cx + (cx > m.l + S * 0.7 ? -8 : 8), y: cy - 7, "text-anchor": cx > m.l + S * 0.7 ? "end" : "start", style: "fill:var(--ink)" }, gEl, trunc(p.a.label, 26));
      hoverable(gEl, p.a.label, () => [
        [`${a}: rank #${p.a.rank}`, fmt(p.a.theta)],
        [`${b}: rank #${p.b.rank}`, fmt(p.b.theta)],
        ["difference", fmt(p.b.theta - p.a.theta)],
      ]);
    }
  }

  function renderTau(g) {
    const t = clear($("tau"));
    if (!g.tau) { el("tr", {}, t).appendChild(document.createTextNode("Needs at least two groups.")); return; }
    const head = el("tr", {}, el("thead", {}, t));
    el("th", {}, head, "");
    g.tau.labels.forEach((l) => el("th", { class: "num" }, head, l));
    const body = el("tbody", {}, t);
    g.tau.labels.forEach((l, i) => {
      const tr = el("tr", {}, body);
      el("th", {}, tr, l);
      g.tau.matrix[i].forEach((v) => {
        if (v === null) { el("td", { class: "cell na" }, tr, "–"); return; }
        const c = diverging(v, 1);
        el("td", { class: "cell", style: `background:${rgb(c)};color:${inkOn(c)}` }, tr, v.toFixed(2));
      });
    });
  }

  // ------------------------------------------------------------------ wiring
  form.addEventListener("change", (e) => { if (!["sx", "sy"].includes(e.target.id)) load(); });
  form.addEventListener("submit", (e) => { e.preventDefault(); load(); });
  $("more").addEventListener("click", () => { showAll = !showAll; render(); });
  $("bump-n").addEventListener("change", () => data?.groups && renderBump(data.overall, data.groups));
  $("sx").addEventListener("change", () => renderScatter(data.groups));
  $("sy").addEventListener("change", () => renderScatter(data.groups));
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => data && render(), 200); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => data && render());
  load();
})();
