/* Bounty Scope Tracker dashboard — vanilla JS, no dependencies */
"use strict";

let PROGRAMS = [], CHANGES = [];
let sortKey = "score", sortDir = -1, platFilter = "all";

const $ = (s) => document.querySelector(s);
const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso), days = Math.floor((Date.now() - d) / 864e5);
  const rel = days <= 0 ? "today" : days === 1 ? "1d ago" :
    days < 30 ? days + "d ago" : days < 365 ? Math.floor(days / 30) + "mo ago" :
    Math.floor(days / 365) + "y ago";
  return `<span title="${iso.slice(0, 10)}">${rel}</span>`;
};
const fmtMoney = (p) => {
  const sym = { usd: "$", eur: "€", gbp: "£" }[p.currency] || (p.currency || "").toUpperCase() + " ";
  const lo = p.avg_bounty_lo, hi = p.avg_bounty_hi;
  if (lo != null) return hi && hi !== lo ? `${sym}${lo}–${sym}${hi}` : `${sym}${lo}`;
  if (p.max_bounty != null) return `≤${sym}${p.max_bounty}`;
  return "—";
};
const PLAT_LABEL = { hackerone: "H1", yeswehack: "YWH", intigriti: "ITI" };

/* ---------- data ---------- */
async function load() {
  const [p, c] = await Promise.all([
    fetch("data/programs.json").then((r) => r.json()),
    fetch("data/changes.json").then((r) => r.json()),
  ]);
  PROGRAMS = p.programs.map((x) => ({
    ...x,
    out_ratio: (x.in_scope_count != null && x.in_scope_count + x.out_scope_count)
      ? x.out_scope_count / (x.in_scope_count + x.out_scope_count) : null,
  }));
  CHANGES = c.changes;
  $("#updated").textContent = new Date(p.generated_at).toLocaleString();
  $("#chg-count").textContent = CHANGES.length;
  renderTable();
  renderFeed();
}

/* ---------- leaderboard ---------- */
function passes(p) {
  if (platFilter !== "all" && p.platform !== platFilter) return false;
  if (p.score < +$("#f-score").value) return false;
  if ($("#f-bounty").checked && !p.offers_bounties) return false;
  if ($("#f-wildcard").checked && !p.wildcard_count) return false;
  if ($("#f-recent").checked) {
    if (!p.newest_asset_at || Date.now() - new Date(p.newest_asset_at) > 90 * 864e5) return false;
  }
  if ($("#f-active").checked) {
    if (!p.last_resolved_at || Date.now() - new Date(p.last_resolved_at) > 60 * 864e5) return false;
  }
  const q = $("#f-search").value.trim().toLowerCase();
  if (q && !p.name.toLowerCase().includes(q) && !p.handle.includes(q)) return false;
  return true;
}

function val(p, k) {
  const v = p[k];
  if (v == null) return sortDir === 1 ? Infinity : -Infinity; // nulls last
  if (k.endsWith("_at")) return new Date(v).getTime();
  return typeof v === "string" ? v.toLowerCase() : v;
}

function renderTable() {
  const rows = PROGRAMS.filter(passes)
    .sort((a, b) => {
      const x = val(a, sortKey), y = val(b, sortKey);
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir * -1 || (a.score - b.score) * -1;
    });
  $("#row-count").textContent = `${rows.length} / ${PROGRAMS.length} programs`;
  const html = rows.map((p) => {
    const hot = p.newest_asset_at && Date.now() - new Date(p.newest_asset_at) < 30 * 864e5;
    return `<tr>
      <td class="num score"><b>${p.score}</b></td>
      <td><span class="plat-badge ${p.platform}">${PLAT_LABEL[p.platform] || p.platform}</span></td>
      <td><a href="${p.url}" target="_blank" rel="noopener">${esc(p.name)}</a>
          ${hot ? '<span class="pill hot">fresh scope</span>' : ""}
          ${p.reports_7d != null && p.reports_7d > 0 ? `<span class="pill warn" title="reports in last 7 days">${p.reports_7d} rep/7d</span>` : ""}
          ${p.submission_state !== "open" ? '<span class="pill warn">' + esc(p.submission_state || "") + "</span>" : ""}</td>
      <td class="num">${fmtMoney(p)}</td>
      <td class="num">${p.in_scope_count != null ? p.in_scope_count : "—"}</td>
      <td class="num">${p.wildcard_count || ""}</td>
      <td class="num">${fmtDate(p.newest_asset_at)}</td>
      <td class="num">${fmtDate(p.last_resolved_at)}</td>
      <td class="num">${p.resolved_delta ? "+" + p.resolved_delta : ""}</td>
      <td class="num">${p.response_efficiency != null ? p.response_efficiency + "%" : "—"}</td>
      <td class="num">${p.out_ratio != null ? (p.out_ratio * 100).toFixed(0) + "%" : "—"}</td>
    </tr>`;
  }).join("");
  $("#rows").innerHTML = html;
}

const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function feedUrl(c) {
  if (c.platform === "yeswehack") return `https://yeswehack.com/programs/${c.handle}`;
  if (c.platform === "intigriti") return `https://app.intigriti.com/programs/${c.handle}`;
  return `https://hackerone.com/${c.handle}`;
}

/* ---------- changes feed ---------- */
function renderFeed() {
  const byDay = {};
  for (const c of CHANGES) {
    const day = c.date.slice(0, 10);
    (byDay[day] = byDay[day] || []).push(c);
  }
  $("#feed").innerHTML = Object.entries(byDay).map(([day, items]) => `
    <h3 class="day">${day}</h3>
    ${items.map((c) => `<div class="feed-item ${c.type}">
      <span class="pill ${c.type}">${{ scope_added: "+ asset", scope_removed: "− asset", new_program: "new program", program_updated: "updated" }[c.type] || c.type}</span>
      <span class="plat-badge ${c.platform || "hackerone"}">${PLAT_LABEL[c.platform] || "H1"}</span>
      <a href="${c.url || feedUrl(c)}" target="_blank" rel="noopener">${esc(c.program)}</a>
      ${c.asset ? `<code>${esc(c.asset)}</code>` : ""}
      ${c.asset_type ? `<span class="muted">${esc(c.asset_type)}</span>` : ""}
    </div>`).join("")}
  `).join("");
}

/* ---------- wiring ---------- */
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll("main section").forEach((s) => { s.hidden = true; });
  $("#tab-" + b.dataset.tab).hidden = false;
}));
document.querySelectorAll("#tbl th").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.k;
  if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = -1; }
  document.querySelectorAll("#tbl th").forEach((x) => x.className =
    x.dataset.k === sortKey ? (x.classList.contains("num") ? "num " : "") +
    (sortDir === -1 ? "sorted-desc" : "sorted-asc") : (x.classList.contains("num") ? "num" : ""));
  renderTable();
}));
document.querySelectorAll("#f-plats .plat").forEach((b) => b.addEventListener("click", () => {
  platFilter = b.dataset.p;
  document.querySelectorAll("#f-plats .plat").forEach((x) => x.classList.toggle("active", x === b));
  renderTable();
}));
["f-score", "f-recent", "f-active", "f-bounty", "f-wildcard", "f-search"].forEach((id) =>
  $("#" + id).addEventListener("input", () => {
    $("#f-score-val").textContent = $("#f-score").value;
    renderTable();
  }));

load().catch((e) => { document.body.insertAdjacentHTML("beforeend",
  `<p style="color:#f66;padding:1em">failed to load data/*.json — run scraper.py first (${e})</p>`); });
