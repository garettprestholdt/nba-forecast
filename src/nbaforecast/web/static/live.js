// Live scores and box scores, straight from ESPN to the visitor's browser (no server involved).
// Display only: nothing here is written anywhere or used by our model. Shows the score, quarter
// and time left on each game card, and on request the box score, play-by-play, and ESPN's own
// in-game win probability (always labeled as ESPN's; our forecasts are made before tip-off).
(function () {
  const games = document.querySelector(".games[data-slate]");
  if (!games || !games.dataset.slate) return;
  const API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba";
  const EVERY = 45 * 1000; // the scoreboard refresh
  const EARLY = 10 * 60 * 1000; // start checking 10 minutes before the first tip-off
  const cards = [...games.querySelectorAll(".matchup[data-game]")];
  const state = {}; // game id -> "pre" | "in" | "post"
  cards.forEach((c) => { state[c.dataset.game] = c.classList.contains("is-final") ? "post" : "pre"; });
  let timer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const pending = () => cards.filter((c) => state[c.dataset.game] !== "post");
  const firstTip = () => Math.min(...pending().map((c) => Date.parse(c.dataset.tip)));

  function label(status) {
    const t = status.type || {};
    if (t.state === "post") return "Final" + (status.period > 4 ? (status.period === 5 ? "/OT" : `/${status.period - 4}OT`) : "");
    if (t.name === "STATUS_HALFTIME") return "Halftime";
    if (t.name === "STATUS_END_PERIOD") return `End of ${status.period > 4 ? "OT" : "Q" + status.period}`;
    const q = status.period > 4 ? (status.period === 5 ? "OT" : `${status.period - 4}OT`) : `Q${status.period}`;
    return `${q} ${status.displayClock}`;
  }

  function paint(event) {
    const card = games.querySelector(`.matchup[data-game="${event.id}"]`);
    if (!card) return;
    const comp = event.competitions[0];
    const status = event.status || comp.status;
    const phase = status.type.state; // pre / in / post
    state[event.id] = phase;
    if (phase === "pre") return;
    card.classList.toggle("is-live", phase === "in");
    card.classList.toggle("is-final", phase === "post");
    const badge = card.querySelector("[data-live-badge]");
    badge.hidden = false;
    badge.textContent = label(status);
    badge.classList.toggle("on", phase === "in");
    card.querySelectorAll(".badge.final").forEach((b) => { if (b.textContent === "Final") b.hidden = true; });
    for (const team of comp.competitors) {
      const el = card.querySelector(`[data-score="${team.homeAway}"]`);
      if (el) { el.textContent = team.score; el.classList.add("score"); el.classList.remove("fav", "dog"); }
    }
    const btn = card.querySelector(".boxbtn");
    if (btn) btn.hidden = false;
  }

  async function poll() {
    clearTimeout(timer);
    if (document.hidden) return; // resumes on visibilitychange
    try {
      const res = await fetch(`${API}/scoreboard?dates=${games.dataset.slate}`, { cache: "no-store" });
      if (res.ok) (await res.json()).events.forEach(paint);
    } catch (e) { /* ESPN unreachable: keep what's shown and try again later */ }
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (!pending().length) return; // every game final: stop
    const wait = Math.max(EVERY, firstTip() - EARLY - Date.now());
    timer = setTimeout(poll, Math.min(wait, 30 * 60 * 1000));
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  if (pending().some((c) => Date.parse(c.dataset.tip) - EARLY <= Date.now())) poll(); else schedule();

  // --- live box score ------------------------------------------------------------------
  const dialog = document.getElementById("box-dialog");
  const body = dialog && dialog.querySelector(".box-body");
  let boxTimer = null, openId = null;
  const COLS = [["MIN", "MIN"], ["PTS", "PTS"], ["REB", "REB"], ["AST", "AST"], ["STL", "STL"], ["BLK", "BLK"],
                ["TO", "TO"], ["FG", "FG"], ["3PT", "3P"], ["FT", "FT"], ["+/-", "+/-"]];

  function teamTable(team) {
    const block = (team.statistics || [])[0] || { names: [], athletes: [], totals: [] };
    const idx = COLS.map(([name]) => block.names.indexOf(name));
    const row = (stats) => idx.map((i) => `<td class="r">${i >= 0 && stats ? esc(stats[i]) : ""}</td>`).join("");
    const players = block.athletes.map((a) => {
      const id = a.athlete && a.athlete.id;
      const name = esc((a.athlete && a.athlete.displayName) || "");
      const who = id ? `<button type="button" class="link" data-player="${esc(id)}">${name}</button>` : name;
      if (a.didNotPlay || !(a.stats && a.stats.length)) {
        return `<tr class="dnp"><td>${who}</td><td colspan="${COLS.length}">${esc((a.reason || "Did not play").toLowerCase())}</td></tr>`;
      }
      return `<tr${a.starter ? ' class="starter"' : ""}><td>${who}${a.starter ? ' <span class="muted">S</span>' : ""}</td>${row(a.stats)}</tr>`;
    }).join("");
    return `<h3 class="pd-sub">${esc(team.team.displayName)}</h3>
      <div class="table-wrap"><table class="data compact">
        <thead><tr><th>Player</th>${COLS.map(([, l]) => `<th class="r">${l}</th>`).join("")}</tr></thead>
        <tbody>${players}<tr class="totals"><td>Team</td>${row(block.totals)}</tr></tbody>
      </table></div>`;
  }

  // Game time elapsed, in seconds, from a period number and a clock like "4:15" or "38.2".
  function elapsed(period, clock) {
    const [m, sec] = String(clock || "0").includes(":") ? clock.split(":").map(Number) : [0, Number(clock)];
    const left = m * 60 + sec;
    const before = period <= 4 ? (period - 1) * 720 : 2880 + (period - 5) * 300;
    return before + (period <= 4 ? 720 : 300) - left;
  }

  function playByPlay(data, abbr, quarter) {
    const plays = (data.plays || []).filter((p) => !quarter || p.period.number === quarter);
    if (!plays.length) return '<p class="empty">No plays yet.</p>';
    const periods = [...new Set((data.plays || []).map((p) => p.period.number))];
    const filters = `<div class="pbp-filter" role="group" aria-label="Quarter">${[0, ...periods].map((q) =>
      `<button type="button" data-q="${q}" aria-pressed="${q === quarter}">${q === 0 ? "All" : q > 4 ? (q === 5 ? "OT" : q - 4 + "OT") : "Q" + q}</button>`).join("")}</div>`;
    const rows = plays.slice().reverse().map((p) => `
      <li class="${p.scoringPlay ? "scoring" : ""}">
        <span class="t">${p.period.number > 4 ? "OT" : "Q" + p.period.number} ${esc(p.clock.displayValue)}</span>
        <span class="tm">${esc(abbr[p.team && p.team.id] || "")}</span>
        <span class="tx">${esc(p.text)}</span>
        <span class="sc">${p.awayScore}–${p.homeScore}</span>
      </li>`).join("");
    return filters + `<ol class="pbp">${rows}</ol>`;
  }

  function winProbability(data, sides) {
    const wp = data.winprobability || [];
    const plays = new Map((data.plays || []).map((p) => [p.id, p]));
    const pts = wp.map((w) => {
      const p = plays.get(w.playId);
      return p ? [elapsed(p.period.number, p.clock.displayValue), w.homeWinPercentage] : null;
    }).filter(Boolean);
    if (pts.length < 2) return '<p class="empty">ESPN\'s win probability appears once the game is under way.</p>';
    const lastPeriod = Math.max(4, ...(data.plays || []).map((p) => p.period.number));
    const total = 2880 + Math.max(0, lastPeriod - 4) * 300;
    const W = 720, H = 260, padL = 44, padR = 12, padT = 14, padB = 26;
    const x = (t) => padL + (t / total) * (W - padL - padR);
    const y = (v) => padT + (1 - v) * (H - padT - padB);
    const line = pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const area = `${line}L${x(pts[pts.length - 1][0]).toFixed(1)},${y(0.5)}L${x(pts[0][0]).toFixed(1)},${y(0.5)}Z`;
    const ticks = [];
    for (let q = 1; q <= lastPeriod; q++) {
      const t = q <= 4 ? (q - 1) * 720 : 2880 + (q - 5) * 300;
      ticks.push(`<line x1="${x(t)}" x2="${x(t)}" y1="${padT}" y2="${H - padB}" class="qline"/><text x="${x(t) + 4}" y="${H - 8}" class="qlab">${q > 4 ? "OT" : "Q" + q}</text>`);
    }
    const last = pts[pts.length - 1][1];
    const home = sides.home.team.abbreviation, away = sides.away.team.abbreviation;
    const lead = last >= 0.5 ? [home, last] : [away, 1 - last];
    return `
      <p class="espn-note"><strong>This is ESPN's win probability, not ours.</strong> It comes from ESPN's own in-game model and
        updates with every play. Our forecasts are made once, before tip-off, and never change.</p>
      <p class="wp-now">ESPN gives <b>${esc(lead[0])}</b> a <b>${Math.round(lead[1] * 100)}%</b> chance right now.</p>
      <svg class="wp" viewBox="0 0 ${W} ${H}" role="img" aria-label="ESPN win probability for ${esc(home)} over the game, now ${Math.round(last * 100)}%">
        <text x="${padL - 8}" y="${y(1) + 4}" class="ylab" text-anchor="end">${esc(home)}</text>
        <text x="${padL - 8}" y="${y(0) + 4}" class="ylab" text-anchor="end">${esc(away)}</text>
        <text x="${padL - 8}" y="${y(0.5) + 4}" class="ylab" text-anchor="end">50%</text>
        ${ticks.join("")}
        <line x1="${padL}" x2="${W - padR}" y1="${y(0.5)}" y2="${y(0.5)}" class="mid"/>
        <path d="${area}" class="wp-area"/>
        <path d="${line}" class="wp-line"/>
        <circle cx="${x(pts[pts.length - 1][0])}" cy="${y(last)}" r="4" class="wp-dot"/>
      </svg>
      <p class="note">Up is ${esc(home)} (home), down is ${esc(away)}. Source: ESPN.</p>`;
  }

  let tab = "box", quarter = 0, lastData = null;

  function renderBox(data) {
    const comp = data.header.competitions[0];
    const status = comp.status;
    const sides = {};
    comp.competitors.forEach((c) => { sides[c.homeAway] = c; });
    const abbr = {};
    comp.competitors.forEach((c) => { abbr[c.team.id] = c.team.abbreviation; });
    const teams = (data.boxscore && data.boxscore.players) || [];
    const order = ["away", "home"].map((h) => teams.find((t) => String(t.team.id) === String(sides[h].team.id))).filter(Boolean);
    const tabs = [["box", "Box score"], ["pbp", "Play-by-play"], ["wp", "Win probability (ESPN)"]];
    const panel = tab === "box"
      ? (order.length ? order.map(teamTable).join("") : '<p class="empty">The box score appears once the game starts.</p>')
      : tab === "pbp" ? playByPlay(data, abbr, quarter) : winProbability(data, sides);
    body.innerHTML = `
      <header class="box-head">
        <p class="kicker">${status.type.state === "in" ? '<span class="dot"></span> Live · ' : ""}${esc(label(status))}</p>
        <h2 id="box-title" class="display">${esc(sides.away.team.abbreviation)} ${esc(sides.away.score ?? "")}
          <span class="muted">@</span> ${esc(sides.home.team.abbreviation)} ${esc(sides.home.score ?? "")}</h2>
        <p class="small">From ESPN${status.type.state === "in" ? ", refreshed every 45 seconds" : ""}. Tap a player for their season.</p>
      </header>
      <div class="pd-tabs box-tabs" role="tablist">${tabs.map(([id, l]) => `<button role="tab" data-tab="${id}" aria-selected="${tab === id}">${l}</button>`).join("")}</div>
      <div class="box-teams" role="tabpanel">${panel}</div>`;
  }

  async function loadBox(id) {
    clearTimeout(boxTimer);
    try {
      const res = await fetch(`${API}/summary?event=${encodeURIComponent(id)}`, { cache: "no-store" });
      lastData = await res.json();
      if (openId !== id) return;
      renderBox(lastData);
      const live = lastData.header.competitions[0].status.type.state === "in";
      if (live && !document.hidden) boxTimer = setTimeout(() => loadBox(id), EVERY);
    } catch (e) {
      body.innerHTML = '<p class="empty">ESPN isn\'t responding right now. Try again in a moment.</p>';
    }
  }

  if (dialog) {
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-box]");
      if (!b) return;
      openId = b.dataset.box;
      tab = "box"; quarter = 0; lastData = null;
      body.innerHTML = '<p class="empty">Loading…</p>';
      dialog.showModal();
      loadBox(openId);
    });
    dialog.querySelector(".pd-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { openId = null; clearTimeout(boxTimer); });
    body.addEventListener("click", (e) => {
      const t = e.target.closest("[data-tab]");
      const q = e.target.closest("[data-q]");
      if (t) { tab = t.dataset.tab; quarter = 0; }
      else if (q) { quarter = Number(q.dataset.q); }
      else return;
      if (lastData) renderBox(lastData);
    });
  }
})();
