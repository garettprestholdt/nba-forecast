// Player pop-up (any element with data-player="<id>") and small page interactions.
// Player data lives in players/<id>.json, written at build time.
(function () {
  const dialog = document.getElementById("player-dialog");
  const body = dialog && dialog.querySelector(".pd-body");
  const cache = {};

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const pct = (v) => (v == null ? "–" : (v * 100).toFixed(1) + "%");
  const num = (v, d = 1) => (v == null || v === "" ? "–" : Number(v).toFixed(d));
  const signed = (v) => (v == null || v === "" ? "–" : (Number(v) > 0 ? "+" : "") + Number(v));
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });

  function header(p) {
    const team = p.team ? `<a href="team-${esc(p.team.abbr.toLowerCase())}.html">${esc(p.team.name)}</a>` : "Free agent";
    const status = p.status ? `<span class="tag ${p.status === "Out" ? "out" : "gtd"}">${esc(p.status)}</span>` : "";
    const line = p.line || {};
    const proj = p.projection;
    const chips = [
      ["Projected Game Score", proj ? num(proj.score) : "–", proj ? `in ~${num(proj.minutes, 0)} min` : "no NBA minutes yet"],
      ["Game Score this season", line.g ? num(line.gmsc) : "–", line.g ? `${line.g} games` : "hasn't played yet"],
      ["True shooting", line.g ? pct(line.ts_pct) : "–", "points per shot, free throws included"],
      ["Usage", line.g && line.usg_pct != null ? line.usg_pct.toFixed(1) + "%" : "–", "share of team plays they finish"],
    ];
    return `
      <header class="pd-head">
        <div class="pd-photo">${p.headshot ? `<img src="${esc(p.headshot)}" alt="" onerror="this.remove()">` : ""}</div>
        <div class="pd-id">
          <p class="kicker">${team}${p.position ? " · " + esc(p.position) : ""}${p.jersey ? " · #" + esc(p.jersey) : ""}</p>
          <h2 id="pd-title" class="display">${esc(p.name)} ${status}</h2>
          ${bio(p)}
          <div class="pd-chips">${chips.map(([k, v, n]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("")}</div>
        </div>
      </header>`;
  }

  function bio(p) {
    const b = p.bio || {};
    const bits = [b.age != null ? `Age <b>${b.age}</b>` : "", b.height ? `<b>${esc(b.height)}</b>` : "", b.weight ? `<b>${b.weight}</b> lb` : ""].filter(Boolean);
    return bits.length ? `<p class="pd-bio">${bits.join(" · ")}</p>` : "";
  }

  // Profile tab: impact ratings (RAPM), defensive role, and where they shoot from.
  function profile(p) {
    const imp = p.impact;
    const bar = (v) => {
      const w = Math.min(50, (Math.abs(v) / 5) * 50);
      return `<span class="imp-track"><span class="${v >= 0 ? "pos" : "neg"}" style="width:${w}%"></span></span>`;
    };
    const fmt = (v) => (Math.abs(v) < 0.05 ? "0.0" : (v > 0 ? "+" : "−") + Math.abs(v).toFixed(1));
    const row = (label, v, pctl) => `<div class="imp-row"><span class="k">${label}</span>${bar(v)}<span class="v">${fmt(v)}</span>
      ${pctl != null && !imp.small_sample ? `<span class="pctl">Better than ${pctl}% of regulars</span>` : ""}</div>`;
    const left = imp
      ? `<h3 class="pd-sub">Impact per 100 possessions</h3>
         <div class="imp">${row("Offense", imp.o, imp.o_pct)}${row("Defense", imp.d, imp.d_pct)}${row("Overall", imp.total, null)}</div>
         <p class="note">How many more points their team scores (offense) or fewer it allows (defense) with them on the floor, against an
           average player, after accounting for the other nine players: regularized adjusted plus-minus (RAPM) from every lineup over
           ${imp.small_sample ? `only ${imp.poss.toLocaleString()} possessions, so it leans hard toward average` : "the last two seasons"}.
           The box score can't see most defense; this can. Plain fit ${fmt(imp.fits.plain[0])} / ${fmt(imp.fits.plain[1])},
           with a box-score prior ${fmt(imp.fits.box_prior[0])} / ${fmt(imp.fits.box_prior[1])}; shown is a 40/60 blend.</p>`
      : `<p class="empty">No lineup data for this player yet.</p>`;
    const tags = (p.tags || []).length
      ? `<div class="dtags">${p.tags.map((t) => `<span class="dtag">${esc(t)}</span>`).join("")}</div>`
      : `<div class="dtags"><span class="dtag none">No defensive role tag</span></div>`;
    const seasons = Object.keys(p.shots || {}).sort();
    let shots = `<p class="empty">Not enough shots on record for a chart (60 in a season).</p>`;
    if (seasons.length && window.NBAF && window.NBAF.court) {
      shots = `<div class="shots-head"><h3 class="pd-sub" style="margin:0">Where they shoot</h3>${seasons.length > 1
        ? `<div class="seg small" role="group" aria-label="Season">${seasons.map((s) => `<button type="button" data-pseason="${s}" aria-pressed="false">${s}</button>`).join("")}</div>` : ""}</div>
        <div class="pd-court" data-seasons="${seasons.join(",")}"></div>${window.NBAF.courtLegend}`;
    }
    return `<div class="prof"><div>${left}<h3 class="pd-sub">Defensive role</h3>${tags}
      <p class="note">From size and blocks / steals per 36 minutes, given only when the defense rating agrees.</p></div><div>${shots}</div></div>`;
  }

  function drawCourt(p, season) {
    const box = body.querySelector(".pd-court");
    if (!box) return;
    box.innerHTML = window.NBAF.court(p.shots[season], p.league[season], `${p.name}, ${season}`) +
      `<p class="court-sum">${window.NBAF.courtSummary(p.shots[season])}</p>`;
    body.querySelectorAll("[data-pseason]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.pseason === season));
  }

  function gameLog(p) {
    if (!p.games.length) return `<p class="empty">No games yet this season.</p>`;
    const rows = p.games.slice().reverse().map((g) => g.played
      ? `<tr><td>${day(g.date)}</td><td>${g.home ? "vs" : "@"} ${esc(g.opp)}</td><td>${esc(g.result)}</td>
           <td class="r">${g.minutes}</td><td class="r"><b>${g.pts}</b></td><td class="r">${g.reb}</td><td class="r">${g.ast}</td>
           <td class="r">${g.stl}</td><td class="r">${g.blk}</td><td class="r">${g.tov}</td>
           <td class="r">${g.fgm}-${g.fga}</td><td class="r">${g.fg3m}-${g.fg3a}</td><td class="r">${g.ftm}-${g.fta}</td>
           <td class="r">${signed(g.pm)}</td><td class="r"><b>${num(g.gmsc)}</b></td></tr>`
      : `<tr class="dnp"><td>${day(g.date)}</td><td>${g.home ? "vs" : "@"} ${esc(g.opp)}</td><td>${esc(g.result)}</td><td colspan="12">Did not play${g.dnp ? " · " + esc(g.dnp.toLowerCase()) : ""}</td></tr>`
    ).join("");
    return `<div class="table-wrap"><table class="data compact">
      <thead><tr><th>Date</th><th>Opp</th><th>Result</th><th class="r">MIN</th><th class="r">PTS</th><th class="r">REB</th><th class="r">AST</th>
      <th class="r">STL</th><th class="r">BLK</th><th class="r">TOV</th><th class="r">FG</th><th class="r">3P</th><th class="r">FT</th><th class="r">+/-</th><th class="r">GmSc</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function career(p) {
    if (!p.career.length) return `<p class="empty">No NBA seasons on record yet.</p>`;
    const rows = p.career.slice().reverse().map((s) => `<tr${s.season === p.season ? ' class="now"' : ""}>
      <td>${esc(s.season)}</td><td>${esc(s.team)}</td><td class="r">${s.g}</td><td class="r">${num(s.min)}</td>
      <td class="r"><b>${num(s.pts)}</b></td><td class="r">${num(s.reb)}</td><td class="r">${num(s.ast)}</td>
      <td class="r">${num(s.stl)}</td><td class="r">${num(s.blk)}</td>
      <td class="r">${pct(s.fg_pct)}</td><td class="r">${pct(s.fg3_pct)}</td><td class="r">${pct(s.ft_pct)}</td>
      <td class="r adv">${pct(s.ts_pct)}</td><td class="r adv">${pct(s.efg_pct)}</td><td class="r adv">${s.usg_pct == null ? "–" : s.usg_pct.toFixed(1) + "%"}</td>
      <td class="r adv">${num(s.ast_tov, 2)}</td><td class="r adv">${num(s.pts36)}</td><td class="r adv">${s.pm == null ? "–" : signed(s.pm)}</td>
      <td class="r"><b>${num(s.gmsc)}</b></td></tr>`).join("");
    return `<div class="table-wrap"><table class="data compact">
      <thead><tr><th>Season</th><th>Team</th><th class="r">G</th><th class="r">MIN</th><th class="r">PTS</th><th class="r">REB</th><th class="r">AST</th>
      <th class="r">STL</th><th class="r">BLK</th><th class="r">FG%</th><th class="r">3P%</th><th class="r">FT%</th>
      <th class="r adv" title="True shooting: points per shot, counting free throws">TS%</th>
      <th class="r adv" title="Effective FG%: a three counts as 1.5 made shots">eFG%</th>
      <th class="r adv" title="Usage: share of team plays (shots, free throws, turnovers) they finish while on the floor">USG%</th>
      <th class="r adv" title="Assists per turnover">AST/TO</th>
      <th class="r adv" title="Points per 36 minutes">PTS/36</th>
      <th class="r adv" title="Average plus-minus: team point margin while they're on the floor">+/-</th>
      <th class="r">GmSc</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">Per game, regular season. Shaded columns are advanced stats; hover a heading for what it means.</p>`;
  }

  function gameScore(p) {
    const parts = p.gmsc_parts;
    const intro = `<p><strong>Game Score</strong> is John Hollinger's way to sum up a box score in one number,
      on the same scale as points: about <strong>10</strong> is an average game, <strong>20</strong> a very good one,
      <strong>30+</strong> a star's big night. It adds points and the other things that help a team, then subtracts
      the misses, fouls and turnovers that cost it.</p>`;
    if (!parts) return intro + `<p class="empty">The breakdown appears after their first game this season.</p>`;
    const total = parts.reduce((a, x) => a + x.points, 0);
    const max = Math.max(...parts.map((x) => Math.abs(x.points)), 1);
    const rows = parts.map((x) => `<li><span class="lb">${esc(x.label)} <span class="muted">${x.per_game} × ${x.weight > 0 ? "+" : "−"}${Math.abs(x.weight)}</span></span>
      <span class="gbar"><span class="${x.points >= 0 ? "pos" : "neg"}" style="width:${(Math.abs(x.points) / max) * 50}%"></span></span>
      <span class="gv">${x.points > 0 ? "+" : ""}${x.points.toFixed(1)}</span></li>`).join("");
    return intro + `<h3 class="pd-sub">Where ${esc(p.name.split(" ").slice(-1)[0])}'s ${total.toFixed(1)} comes from this season (per game)</h3>
      <ul class="gparts">${rows}</ul>
      <p class="note">Each stat times its weight; they add up to the average Game Score. On the Tonight page, a player's
      projected Game Score is the model's production per minute times their usual minutes.</p>`;
  }

  function render(p) {
    const tabs = [["profile", "Profile", profile(p)], ["season", "This season", gameLog(p)], ["career", "Career", career(p)], ["gmsc", "Game Score", gameScore(p)]];
    body.innerHTML = header(p) + `
      <div class="pd-tabs" role="tablist">${tabs.map(([id, label], i) => `<button role="tab" id="t-${id}" aria-controls="p-${id}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">${label}</button>`).join("")}</div>
      ${tabs.map(([id, , html], i) => `<section role="tabpanel" id="p-${id}" aria-labelledby="t-${id}"${i ? " hidden" : ""}>${html}</section>`).join("")}`;
    const buttons = [...body.querySelectorAll('[role="tab"]')];
    buttons.forEach((b, i) => {
      b.addEventListener("click", () => select(i));
      b.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          const next = (i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length;
          select(next); buttons[next].focus();
        }
      });
    });
    const seasons = Object.keys(p.shots || {}).sort();
    if (seasons.length && window.NBAF && window.NBAF.court) {
      // this season once there's a real sample, else whichever season has more shots
      const shots = (k) => Object.values(p.shots[k]).reduce((a, z) => a + z[0], 0);
      const latest = seasons[seasons.length - 1];
      drawCourt(p, shots(latest) >= 200 ? latest : seasons.reduce((a, b) => (shots(b) > shots(a) ? b : a)));
      body.querySelectorAll("[data-pseason]").forEach((b) => b.addEventListener("click", () => drawCourt(p, b.dataset.pseason)));
    }
    function select(i) {
      buttons.forEach((b, j) => { b.setAttribute("aria-selected", i === j); b.tabIndex = i === j ? 0 : -1; });
      body.querySelectorAll('[role="tabpanel"]').forEach((panel, j) => { panel.hidden = i !== j; });
    }
  }

  async function open(id) {
    if (!dialog) return;
    body.innerHTML = `<p class="empty">Loading…</p>`;
    if (!dialog.open) dialog.showModal();
    try {
      cache[id] = cache[id] || (await (await fetch(`players/${id}.json`)).json());
      render(cache[id]);
    } catch (e) {
      body.innerHTML = `<p class="empty">This player's stats aren't available yet.</p>`;
    }
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-player]");
    if (el) { e.preventDefault(); open(el.dataset.player); }
  });
  if (dialog) {
    dialog.querySelector(".pd-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });  // backdrop
  }

  // Charts inside a closed <details> render at zero width; redraw them when it opens.
  document.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) window.dispatchEvent(new Event("resize"));
  }));

  // History page: filter the game log by team.
  const filter = document.getElementById("team-filter");
  if (filter) {
    filter.addEventListener("change", () => {
      const v = filter.value;
      let shown = 0, right = 0;
      document.querySelectorAll("#log tbody tr").forEach((tr) => {
        const on = !v || tr.dataset.teams.split(" ").includes(v);
        tr.hidden = !on;
        if (on) { shown += 1; right += tr.dataset.correct === "1" ? 1 : 0; }
      });
      document.getElementById("filter-summary").textContent = shown
        ? `${right} of ${shown} picks right (${Math.round((100 * right) / shown)}%)` : "No games yet";
    });
  }
})();
