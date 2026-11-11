// Tenancy Lab dashboard. Plain JS, no build step. Reads the read-only /admin endpoints.

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const PATTERN_COLOR = { pooled: "var(--s1)", bridge: "var(--s2)", silo: "var(--s3)" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (n, d = 0) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const ms = (v) => (v >= 100 ? fmt(v) : v >= 10 ? fmt(v, 1) : fmt(v, 2)) + " ms";

async function get(path) {
  try {
    const r = await fetch(path);
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

// Tooltip shared by all charts.
const tip = $("tip");
function bindTip(node, html) {
  node.addEventListener("mousemove", (ev) => {
    tip.innerHTML = html;
    tip.hidden = false;
    const x = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + "px";
    tip.style.top = ev.clientY + 12 + "px";
  });
  node.addEventListener("mouseleave", () => (tip.hidden = true));
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// Bar with a 4px rounded data end and a square baseline.
function barPath(x, y, w, h, r = 4) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function legend(target, items) {
  target.innerHTML = items.map(([label, color]) => `<span style="--c:${color}">${esc(label)}</span>`).join("");
}

function tiles(tenants, iso, noisy) {
  const by = { pooled: 0, bridge: 0, silo: 0 };
  tenants.forEach((t) => (by[t.pattern] = (by[t.pattern] || 0) + 1));
  const clean = iso ? iso.runs.filter((r) => !r.planted_bug) : [];
  const bug = iso ? iso.runs.find((r) => r.planted_bug) : null;
  const attacks = clean.reduce((a, r) => a + r.attacks, 0);
  const leaks = clean.reduce((a, r) => a + r.leaks, 0);
  const pooled = noisy?.results.filter((r) => r.pattern === "pooled") || [];
  const off = pooled.find((r) => !r.mitigated);
  const on = pooled.find((r) => r.mitigated);
  const cards = [
    ["Tenants", fmt(tenants.length), `${by.pooled} pooled, ${by.bridge} bridge, ${by.silo} silo`],
    ["Cross-tenant leaks", iso ? fmt(leaks) : "n/a", iso ? `${attacks} attacks on 3 backends` : "run make bench"],
    ["Planted bug", bug ? (bug.leaks ? "caught" : "missed") : "n/a", bug ? `${bug.leaks} leaking attacks flagged` : ""],
    ["Pooled small p99", off ? ms(off.small_p99_ms) : "n/a", on ? `${ms(on.small_p99_ms)} with tenant limits` : ""],
  ];
  $("tiles").innerHTML = cards
    .map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`)
    .join("");
}

function tenantTable(tenants) {
  const head = `<tr><th>Tenant</th><th>Pattern</th><th>Tier</th><th class="num">Rows</th><th class="num">Tasks</th><th class="num">Schema</th><th>Status</th><th class="num">Requests</th></tr>`;
  const rows = tenants
    .map((t) => `<tr><td>${esc(t.name)}<br><span class="note">${esc(t.id)}</span></td>
      <td><span class="chip ${esc(t.pattern)}">${esc(t.pattern)}</span></td><td>${esc(t.tier)}</td>
      <td class="num">${fmt(t.rows)}</td><td class="num">${fmt(t.counts?.tasks ?? 0)}</td>
      <td class="num">v${esc(t.schema_version)}</td><td>${esc(t.status)}</td>
      <td class="num">${fmt(t.traffic?.requests ?? 0)}</td></tr>`)
    .join("");
  $("tenants").innerHTML = head + rows;
}

function noisyChart(data) {
  const box = $("noisy");
  if (!data) {
    box.textContent = "No results yet. Run make bench.";
    return;
  }
  const c = data.config;
  $("noisy-note").textContent =
    `One large tenant (${fmt(c.large_tasks)} tasks) runs heavy reports on ${c.report_threads} threads while ${c.small_tenants} small tenants work. ` +
    `Pooled and bridge share ${c.shared_pool} connections; silo gives each tenant ${c.silo_pool}. Limits: ${c.mitigation.concurrency_cap} in flight per tenant, ` +
    `${c.mitigation.rate_tokens_per_s} tokens/s, a report costs ${c.mitigation.report_cost_tokens}. Each panel has its own y axis.`;
  legend($("noisy-legend"), [["Limits off", "var(--s2)"], ["Tenant limits on", "var(--s1)"]]);
  box.innerHTML = "";
  for (const pattern of ["pooled", "bridge", "silo"]) {
    const off = data.results.find((r) => r.pattern === pattern && !r.mitigated);
    const on = data.results.find((r) => r.pattern === pattern && r.mitigated);
    if (!off || !on) continue;
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.innerHTML = `<h3>${pattern}</h3><p class="cap">p99 ${ms(off.small_p99_ms)} to ${ms(on.small_p99_ms)}</p>`;
    box.appendChild(panel);
    const W = 300, H = 190, L = 44, R = 8, T = 16, B = 26;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${pattern} small tenant latency` }, panel);
    const top = Math.max(off.small_p99_ms, on.small_p99_ms);
    const step = niceMax(top / 4);
    const ticks = Math.ceil(top / step);
    const max = step * ticks;
    const y = (v) => T + (H - T - B) * (1 - v / max);
    for (let i = 0; i <= ticks; i++) {
      const v = step * i;
      el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: i ? "gridline" : "axis" }, svg);
      el("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = fmt(v, v < 10 && v % 1 ? 1 : 0);
    }
    el("text", { x: 2, y: 10 }, svg).textContent = "ms";
    const groups = [["p50", "small_p50_ms"], ["p99", "small_p99_ms"]];
    const gw = (W - L - R) / groups.length;
    const bw = Math.min(46, gw / 3);
    groups.forEach(([label, key], gi) => {
      const cx = L + gw * gi + gw / 2;
      [[off, "var(--s2)", "Limits off"], [on, "var(--s1)", "Limits on"]].forEach(([r, color, name], bi) => {
        const v = r[key];
        const x = cx - bw - 1 + bi * (bw + 2);
        const h = Math.max(1, H - B - y(v));
        const p = el("path", { d: barPath(x, H - B - h, bw, h), fill: color }, svg);
        el("text", { x: x + bw / 2, y: H - B - h - 4, "text-anchor": "middle", class: "val" }, svg).textContent = ms(v).replace(" ms", "");
        const hit = el("rect", { x: x - 1, y: T, width: bw + 2, height: H - T - B, fill: "transparent" }, svg);
        const html = `<b>${pattern}, ${name}</b><br>${label}: ${ms(v)}<br>small tenant ops: ${fmt(r.small_ops)}<br>reports done: ${fmt(r.reports)}, rejected: ${fmt(r.reports_rejected)}`;
        bindTip(hit, html);
        bindTip(p, html);
      });
      el("text", { x: cx, y: H - 8, "text-anchor": "middle" }, svg).textContent = label;
    });
  }
}

function migrations(runs, effort) {
  const box = $("migrations");
  const latest = {};
  runs.forEach((r) => (latest[r.pattern] = r));
  box.innerHTML = Object.values(latest)
    .map((r) => {
      const pct = Math.round(r.progress * 100);
      const cls = r.status === "done" ? "ok" : r.status === "failed" ? "leak" : "warn";
      const rec = r.recovered_units.length ? `, recovered after retry: ${r.recovered_units.map(esc).join(", ")}` : "";
      const failed = r.failed_units.length ? `, failed: ${r.failed_units.map((f) => esc(f.unit)).join(", ")}` : "";
      return `<div class="run"><span class="chip ${esc(r.pattern)}">${esc(r.pattern)}</span>
        <div class="progress" title="${pct}%"><div style="width:${pct}%"></div></div>
        <span class="${cls}">${esc(r.status)} ${r.done}/${r.units}</span>
        <div class="meta">to v${r.target}: ${r.units} unit${r.units === 1 ? "" : "s"} covering ${r.tenants} tenants, ${r.ddl_statements} DDL statements, ${r.attempts} run attempt${r.attempts === 1 ? "" : "s"}${rec}${failed}</div></div>`;
    })
    .join("");
  if (!effort) {
    $("effort").innerHTML = "<tr><td>Run make bench.</td></tr>";
    return;
  }
  $("effort").innerHTML =
    `<tr><th>Pattern</th><th class="num">Units</th><th class="num">DDL</th><th class="num">Time</th><th class="num">Tenants locked per step</th><th>Resume</th></tr>` +
    effort.results
      .map((r) => `<tr><td><span class="chip ${r.pattern}">${r.pattern}</span></td><td class="num">${fmt(r.units)}</td>
        <td class="num">${fmt(r.ddl_statements)}</td><td class="num">${ms(r.migration_ms)}</td><td class="num">${fmt(r.tenants_blocked_per_unit)}</td>
        <td class="${r.all_on_latest ? "ok" : "leak"}">${r.all_on_latest ? "all on v" + effort.latest : "incomplete"}</td></tr>`)
      .join("");
}

function costChart(model) {
  const rows = model.rows;
  legend($("cost-legend"), ["pooled", "bridge", "silo"].map((p) => [p, PATTERN_COLOR[p]]));
  const W = 520, H = 280, L = 52, R = 56, T = 12, B = 30;
  const box = $("cost");
  box.innerHTML = "";
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Cost per tenant by tenant count" }, box);
  const xs = [...new Set(rows.map((r) => r.tenants))];
  const vals = rows.map((r) => r.per_tenant_usd);
  const y0 = Math.floor(Math.log10(Math.min(...vals))), y1 = Math.ceil(Math.log10(Math.max(...vals)));
  const lx0 = Math.log10(xs[0]), lx1 = Math.log10(xs[xs.length - 1]);
  const x = (n) => L + ((Math.log10(n) - lx0) / (lx1 - lx0)) * (W - L - R);
  const y = (v) => T + (H - T - B) * (1 - (Math.log10(v) - y0) / (y1 - y0));
  for (let d = y0; d <= y1; d++) {
    el("line", { x1: L, x2: W - R, y1: y(10 ** d), y2: y(10 ** d), class: d === y0 ? "axis" : "gridline" }, svg);
    el("text", { x: L - 6, y: y(10 ** d) + 4, "text-anchor": "end" }, svg).textContent = "$" + fmt(10 ** d);
  }
  xs.forEach((n) => (el("text", { x: x(n), y: H - 10, "text-anchor": "middle" }, svg).textContent = fmt(n)));
  for (const p of ["pooled", "bridge", "silo"]) {
    const pts = rows.filter((r) => r.pattern === p);
    el("path", { d: pts.map((r, i) => `${i ? "L" : "M"}${x(r.tenants)},${y(r.per_tenant_usd)}`).join(""), fill: "none", stroke: PATTERN_COLOR[p], "stroke-width": 2 }, svg);
    pts.forEach((r) => {
      el("circle", { cx: x(r.tenants), cy: y(r.per_tenant_usd), r: 4, fill: PATTERN_COLOR[p], stroke: "var(--surface)", "stroke-width": 2 }, svg);
      const hit = el("circle", { cx: x(r.tenants), cy: y(r.per_tenant_usd), r: 12, fill: "transparent" }, svg);
      bindTip(hit, `<b>${p}, ${fmt(r.tenants)} tenants</b><br>$${fmt(r.per_tenant_usd, 2)} per tenant<br>$${fmt(r.total_usd)} per month total<br>${fmt(r.instances)} instances, ${fmt(r.connections)} connections<br>ops ${fmt(r.ops_hours, 1)} h`);
    });
    const last = pts[pts.length - 1];
    el("text", { x: x(last.tenants) + 8, y: y(last.per_tenant_usd) + 4, class: "val" }, svg).textContent = p;
  }
  $("cost-table").innerHTML =
    `<tr><th>Tenants</th>${["pooled", "bridge", "silo"].map((p) => `<th class="num">${p}</th>`).join("")}</tr>` +
    xs
      .map((n) => `<tr><td>${fmt(n)}</td>${["pooled", "bridge", "silo"].map((p) => {
        const r = rows.find((q) => q.pattern === p && q.tenants === n);
        return `<td class="num">$${fmt(r.per_tenant_usd, 2)}</td>`;
      }).join("")}</tr>`)
      .join("");
}

function isolation(iso) {
  if (!iso) {
    $("isolation").innerHTML = "<tr><td>No results yet. Run make bench.</td></tr>";
    return;
  }
  const runs = iso.runs;
  const clean = runs.filter((r) => !r.planted_bug);
  $("iso-note").textContent =
    `${clean.reduce((a, r) => a + r.attacks, 0)} attacks across ${clean.length} backends found ${clean.reduce((a, r) => a + r.leaks, 0)} leaks. ` +
    `The last column turns on a planted bug (a pooled search query that forgets the tenant filter); the suite must flag it.`;
  const head = `<tr><th>Attack</th><th>Category</th>${runs.map((r) => `<th>${esc(r.backend)}${r.planted_bug ? " + planted bug" : ""}</th>`).join("")}</tr>`;
  const body = runs[0].results
    .map((row, i) => `<tr><td>${esc(row.attack.replaceAll("_", " "))}</td><td>${esc(row.category)}</td>${runs
      .map((r) => {
        const x = r.results[i];
        const code = x.status ? ` ${x.status}` : "";
        return x.leaked ? `<td class="leak" title="${esc(x.detail)}">LEAK${code}</td>` : `<td class="ok" title="${esc(x.detail)}">blocked${code}</td>`;
      })
      .join("")}</tr>`)
    .join("");
  $("isolation").innerHTML = head + body;
}

function events(list) {
  $("events").innerHTML = list
    .map((e) => {
      const d = e.detail || {};
      const bits = Object.entries(d)
        .filter(([k]) => !["source_checksums", "target_checksums", "export_path"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "string" && v.length > 16 ? v.slice(0, 12) + "..." : v}`)
        .join(" ");
      return `<li><span class="ts">${esc(e.ts.slice(0, 19).replace("T", " "))}</span><b>${esc(e.kind)}</b> ${esc(e.tenant_id || "")} <span class="note">${esc(bits)}</span></li>`;
    })
    .join("");
}

async function main() {
  const [tenants, runs, effort, noisy, iso, cost, evts] = await Promise.all([
    get("/admin/tenants"), get("/admin/migrations"), get("/admin/migration-effort"), get("/admin/noisy-neighbor"),
    get("/admin/isolation"), get("/admin/cost"), get("/admin/events?limit=15"),
  ]);
  tiles(tenants || [], iso, noisy);
  tenantTable(tenants || []);
  noisyChart(noisy);
  migrations(runs || [], effort);
  if (cost) costChart(cost);
  isolation(iso);
  events(evts || []);
}

main();
