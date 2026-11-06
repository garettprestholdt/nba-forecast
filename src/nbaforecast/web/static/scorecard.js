// Scorecard page: the night picker and each forecast's pop-up (overview, how it works, every
// pick with filters). Games come from the JSON the build embeds (web/scorecard.py); the
// pop-up reuses the player dialog from base.html, which app.js already opens and closes.
(function () {
  const el = document.getElementById("scorecard-data");
  if (!el) return;
  const S = JSON.parse(el.textContent);
  const MODELS = {
    elo: { name: "Elo", short: "Elo", role: "Published", cls: "s1",
      lede: "Team ratings built from results and margins. This is the forecast shown on every game." },
    v2: { name: "Player + injury model", short: "Challenger", role: "Challenger", cls: "s3",
      lede: "Elo's ratings plus the players expected to suit up, from that morning's injury report." },
    mkt: { name: "Betting market", short: "Market", role: "Benchmark", cls: "s2",
      lede: "What the sportsbooks implied that morning, with their margin taken out. The bar both models are chasing." },
  };
  const KEYS = Object.keys(MODELS);
  const dialog = document.getElementById("player-dialog");
  const body = dialog && dialog.querySelector(".pd-body");

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // whole percent, halves to even, like the build's `pct` filter (so cards and pop-up agree)
  const pct = (v) => {
    const x = v * 100, r = Math.round(x);
    return (Math.abs(x - Math.trunc(x)) === 0.5 && r % 2 ? r - 1 : r) + "%";
  };
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const won = (g) => g.hs > g.as;
  const right = (g, k) => (g[k] >= 0.5) === won(g);
  const pick = (g, k) => (g[k] >= 0.5 ? g.home : g.away);
  const conf = (g, k) => Math.max(g[k], 1 - g[k]);
  const logLoss = (rows, k) => rows.reduce((a, g) => a - Math.log(won(g) ? g[k] : 1 - g[k]), 0) / rows.length;
  const brier = (rows, k) => rows.reduce((a, g) => a + (g[k] - (won(g) ? 1 : 0)) ** 2, 0) / rows.length;
  const logo = (abbr) => (S.logos[abbr] ? `<img src="${esc(S.logos[abbr])}" alt="" loading="lazy" onerror="this.remove()">` : "");
  const matchup = (g, bold) => `<span class="gm">${logo(g.away)}<span class="${bold && !won(g) ? "w" : ""}">${g.away}</span> @ ${logo(g.home)}<span class="${bold && won(g) ? "w" : ""}">${g.home}</span></span>`;
  const nights = [...new Set(S.games.map((g) => g.date))];  // newest first

  // ---- night by night ------------------------------------------------------------------
  const sel = document.getElementById("night");
  sel.innerHTML = nights.map((d, i) => `<option value="${d}">${day(d)}${i === 0 ? " (latest)" : ""}</option>`).join("");
  function drawNight() {
    const rows = S.games.filter((g) => g.date === sel.value);
    const cell = (g, k) => (g[k] == null ? `<td class="muted">–</td>` : `<td class="pk ${right(g, k) ? "hit" : "miss"}">${pick(g, k)} ${pct(conf(g, k))}</td>`);
    document.querySelector("#night-table tbody").innerHTML = rows.map((g) =>
      `<tr><td>${matchup(g, true)}</td><td>${g.as}–${g.hs}</td>${KEYS.map((k) => cell(g, k)).join("")}</tr>`).join("");
    document.getElementById("night-sum").innerHTML = KEYS.map((k) => {
      const r = rows.filter((g) => g[k] != null);
      return r.length ? `<span><span class="key ${MODELS[k].cls}"></span>${MODELS[k].short} <b>${r.filter((g) => right(g, k)).length}/${r.length}</b></span>` : "";
    }).join("");
  }
  sel.addEventListener("change", drawNight);
  drawNight();

  // ---- pop-up --------------------------------------------------------------------------
  function open(k) {
    const m = MODELS[k];
    const rows = S.games.filter((g) => g[k] != null);
    const r = rows.filter((g) => right(g, k)).length;
    const first = rows[rows.length - 1].date;
    const chips = [
      ["Log-loss", logLoss(rows, k).toFixed(3), "lower is better · coin flip 0.693"],
      ["Picks right", pct(r / rows.length), `${r} of ${rows.length} games`],
      ["Brier score", brier(rows, k).toFixed(3), "lower is better · coin flip 0.250"],
      ["Games", rows.length, `every one since ${day(first)}`],
    ];
    const tabs = [["ov", "Overview", overview(k, rows)], ["how", "How it works", how(k)], ["log", "Every pick", picks(k, rows)]];
    body.innerHTML = `
      <header class="md-head m-${k}">
        <p class="kicker">${m.role} · ${esc(S.season)} season</p>
        <h2 id="pd-title" class="display">${esc(m.name)}</h2>
        <p class="lede">${m.lede}</p>
        <div class="pd-chips">${chips.map(([a, v, n]) => `<div class="stat"><div class="k">${a}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("")}</div>
      </header>
      <div class="pd-tabs" role="tablist">${tabs.map(([id, label], i) => `<button role="tab" id="t-${id}" aria-controls="p-${id}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">${label}</button>`).join("")}</div>
      ${tabs.map(([id, , html], i) => `<section role="tabpanel" id="p-${id}" class="m-${k}" aria-labelledby="t-${id}"${i ? " hidden" : ""}>${html}</section>`).join("")}`;
    const buttons = [...body.querySelectorAll('[role="tab"]')];
    const select = (i) => {
      buttons.forEach((b, j) => { b.setAttribute("aria-selected", i === j); b.tabIndex = i === j ? 0 : -1; });
      body.querySelectorAll('[role="tabpanel"]').forEach((p, j) => { p.hidden = i !== j; });
    };
    buttons.forEach((b, i) => {
      b.addEventListener("click", () => select(i));
      b.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          const next = (i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length;
          select(next); buttons[next].focus();
        }
      });
    });
    wirePicks(k, rows);
    if (!dialog.open) dialog.showModal();
    body.scrollTop = 0;
  }

  function overview(k, rows) {
    const buckets = [[0.5, 0.6, "50–60%"], [0.6, 0.7, "60–70%"], [0.7, 0.8, "70–80%"], [0.8, 1.01, "80%+"]].map(([lo, hi, label]) => {
      const s = rows.filter((g) => conf(g, k) >= lo && conf(g, k) < hi);
      return s.length && { label, n: s.length, said: s.reduce((a, g) => a + conf(g, k), 0) / s.length, right: s.filter((g) => right(g, k)).length / s.length };
    }).filter(Boolean);
    const bar = (t, cls, v) => `<span class="row"><span class="t">${t}</span><span class="track"><span class="f ${cls}" style="width:${v * 100}%"></span></span><span class="v">${pct(v)}</span></span>`;
    const calib = buckets.map((b) => `<li><span class="lb">${b.label} <span class="muted">· ${b.n} game${b.n === 1 ? "" : "s"}</span></span>
      <span class="pair">${bar("Said", "said", b.said)}${bar("Right", "right", b.right)}</span></li>`).join("");
    const line = (g) => `<li><span>${day(g.date)} · ${g.away} @ ${g.home}, ${g.as}–${g.hs}</span><span class="muted">${pick(g, k)} ${pct(conf(g, k))}</span></li>`;
    const misses = rows.filter((g) => !right(g, k)).sort((a, b) => conf(b, k) - conf(a, k)).slice(0, 4);
    const close = rows.filter((g) => right(g, k)).sort((a, b) => conf(a, k) - conf(b, k)).slice(0, 4);
    return `<div class="ov">
      <div><h3 class="pd-sub">When it says X%, how often is it right?</h3>
        <p class="small">If the forecasts are honest, the two bars in each row match. Small groups swing a lot early in the season.</p>
        <ul class="calib">${calib}</ul></div>
      <div><h3 class="pd-sub">Biggest misses</h3><ul class="calls pairs">${misses.map(line).join("") || `<li class="muted">None yet.</li>`}</ul>
        <h3 class="pd-sub">Closest calls it got right</h3><ul class="calls pairs">${close.map(line).join("")}</ul></div></div>`;
  }

  function how(k) {
    const h = S.how[k];
    if (!h) return `<p class="empty">Not available yet.</p>`;
    if (k === "elo") {
      const gap = h.avg_gap / (h.avg_gap + h.home_court);
      const row = (label, note, share) => `<li><span class="lb">${label}<small>${note}</small></span>
        <span class="tr left"><span class="pos" style="width:${share * 100}%"></span></span><span class="v">${pct(share)}</span></li>`;
      return `<h3 class="pd-sub first">What goes into it</h3>
        <ul class="imp-list">${row("Rating gap", "Home team's rating minus the away team's", gap)}
          ${row("Home court", `Learned from the last ${h.window.toLocaleString()} home games, now ${Math.round(h.home_court)} Elo points`, 1 - gap)}</ul>
        <p class="note">Each one's share of a typical forecast's lean this season: the average rating gap was ${Math.round(h.avg_gap)} Elo
          points, against ${Math.round(h.home_court)} for home court. About ${Math.round(h.per_point)} Elo points equal one point on the scoreboard.</p>
        <h3 class="pd-sub">How ratings move</h3>
        <ol class="steps">
          <li><div><b>After every game</b><p>The winner takes rating points from the loser: more for an upset or a big margin, with diminishing returns, and less when a heavy favorite runs up the score (K = ${h.k}).</p></div></li>
          <li><div><b>Each summer</b><p>Every rating moves ${pct(h.regress)} of the way back to average, since rosters change.</p></div></li>
          <li><div><b>The forecast</b><p>Home win chance = 1 / (1 + 10<sup>−(rating gap + home court) / 400</sup>).</p></div></li>
        </ol>`;
    }
    if (k === "v2") {
      const all = h.flatMap((g) => g.inputs);
      const max = Math.max(...all.map((x) => Math.abs(x.weight)));
      const li = (x) => `<li><span class="lb">${esc(x.label)}${x.note ? `<small>${esc(x.note)}</small>` : ""}</span>
        <span class="tr"><span class="${x.weight >= 0 ? "pos" : "neg"}" style="width:${(Math.abs(x.weight) / max) * 50}%"></span></span>
        <span class="v">${x.weight >= 0 ? "+" : "−"}${Math.abs(x.weight).toFixed(2)}</span></li>`;
      return `<h3 class="pd-sub first">The ${all.length} inputs, by weight</h3>
        <p class="small">How much each input moves the forecast when it changes by a typical amount (one standard deviation), in log-odds.
          A bar to the right raises the home team's chance as that input grows; to the left lowers it. Each group's share is its part of the total weight.</p>
        <ul class="imp-list">${h.map((g) => `<li class="grp">${esc(g.group)} · ${pct(g.share)}</li>${g.inputs.map(li).join("")}`).join("")}</ul>
        <p class="note">Logistic regression with a ridge penalty (L2 = ${h[0].l2}), fit on ${h[0].fit[0]} to ${h[0].fit[1]}. Some inputs overlap
          (Elo and net rating; offense and production), and the penalty spreads weight between them, so read a pair's combined weight rather than either one alone.</p>
        <h3 class="pd-sub">Who it counts as out</h3>
        <p class="small">Players listed Out are left out; Day-To-Day players count by the injury watch's chance that they play. It uses only
          the injury report from the morning of the game.</p>`;
    }
    return `<h3 class="pd-sub first">How the market number is built</h3>
      <ol class="steps">
        <li><div><b>Collect the lines</b><p>Each morning's run pulls every US sportsbook's moneyline from The Odds API, once, before any game tips off:
          ${h.min}–${h.max} books per game this season, ${h.avg.toFixed(1)} on average.</p></div></li>
        <li><div><b>Remove the margin</b><p>A book's odds imply chances that add up to more than 100%; the extra is its cut. Each book's two numbers are scaled down to add to 100%.</p>
          <div class="ex"><table>
            <tr><td>Example book</td><td class="r">Home</td><td class="r">Away</td><td class="r">Total</td></tr>
            <tr><td>Odds (decimal)</td><td class="r">1.50</td><td class="r">2.70</td><td></td></tr>
            <tr><td>Implied (1 / odds)</td><td class="r">66.7%</td><td class="r">37.0%</td><td class="r">103.7%</td></tr>
            <tr><td><b>Margin removed</b></td><td class="r"><b>64.3%</b></td><td class="r"><b>35.7%</b></td><td class="r">100%</td></tr></table></div></div></li>
        <li><div><b>Take the median</b><p>The market's chance is the middle value across books, so one book's outlying line can't move it.</p></div></li>
      </ol>
      <p class="note">This is the morning line, not the closing line: what was known when our forecasts were logged, so the comparison is fair.
        Lines keep moving through the day as injury news lands, which is part of why the market is hard to beat.</p>`;
  }

  function picks(k, rows) {
    const teams = [...new Set(rows.flatMap((g) => [g.home, g.away]))].sort();
    const others = KEYS.filter((o) => o !== k);
    const opt = (v, label) => `<option value="${v}">${label}</option>`;
    return `<div class="filters">
        <select data-f="date" aria-label="Night">${opt("", "Every night")}${nights.map((d) => opt(d, day(d))).join("")}</select>
        <select data-f="team" aria-label="Team">${opt("", "All teams")}${teams.map((t) => opt(t, t)).join("")}</select>
        <select data-f="res" aria-label="Result">${opt("", "Right and wrong")}${opt("1", "Right")}${opt("0", "Wrong")}</select>
        <select data-f="conf" aria-label="Confidence">${opt("", "Any confidence")}${opt("0.5-0.6", "Toss-ups (50–60%)")}${opt("0.6-0.7", "Leans (60–70%)")}${opt("0.7-1.01", "Strong (70%+)")}</select>
        <select data-f="side" aria-label="Side picked">${opt("", "Home or away pick")}${opt("h", "Picked the home team")}${opt("a", "Picked the away team")}</select>
        <select data-f="vs" aria-label="Agreement">${opt("", "With or against others")}${others.map((o) => opt(o, `Picked against ${o === "elo" ? "Elo" : o === "v2" ? "the challenger" : "the market"}`)).join("")}</select>
        <span class="sum" data-sum aria-live="polite"></span>
      </div>
      <div class="table-wrap"><table class="data compact"><thead><tr><th>Date</th><th>Game</th><th>Pick</th>
        ${others.map((o) => `<th class="wide-only"><span class="key ${MODELS[o].cls}"></span>${MODELS[o].short}</th>`).join("")}<th>Final</th><th></th></tr></thead>
        <tbody data-log></tbody></table></div>`;
  }

  function wirePicks(k, rows) {
    const f = Object.fromEntries([...body.querySelectorAll("[data-f]")].map((s) => [s.dataset.f, s]));
    const others = KEYS.filter((o) => o !== k);
    const draw = () => {
      const [lo, hi] = (f.conf.value || "0-2").split("-").map(Number);
      const vs = f.vs.value;
      const shown = rows.filter((g) => (!f.date.value || g.date === f.date.value)
        && (!f.team.value || g.home === f.team.value || g.away === f.team.value)
        && (!f.res.value || right(g, k) === (f.res.value === "1"))
        && conf(g, k) >= lo && conf(g, k) < hi
        && (!f.side.value || (g[k] >= 0.5) === (f.side.value === "h"))
        && (!vs || (g[vs] != null && (g[vs] >= 0.5) !== (g[k] >= 0.5))));
      const r = shown.filter((g) => right(g, k)).length;
      body.querySelector("[data-sum]").textContent = shown.length
        ? `${r} of ${shown.length} right · log-loss ${logLoss(shown, k).toFixed(3)}` : "No games match";
      body.querySelector("[data-log]").innerHTML = shown.map((g) => `<tr><td>${day(g.date)}</td><td>${matchup(g, false)}</td>
        <td><b>${pick(g, k)} ${pct(conf(g, k))}</b></td>
        ${others.map((o) => `<td class="wide-only muted">${g[o] == null ? "–" : `${pick(g, o)} ${pct(conf(g, o))}`}</td>`).join("")}
        <td>${g.as}–${g.hs}</td><td><span class="call ${right(g, k) ? "hit" : "miss"}">${right(g, k) ? "✓ Right" : "✗ Wrong"}</span></td></tr>`).join("");
    };
    Object.values(f).forEach((s) => s.addEventListener("change", draw));
    draw();
  }

  document.addEventListener("click", (e) => {
    const card = e.target.closest("[data-model]");
    if (card && dialog) open(card.dataset.model);
  });
})();
