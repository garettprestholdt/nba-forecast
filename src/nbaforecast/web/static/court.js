// Half-court shot chart: nine zones, each labeled with FG% and its share of the shots, colored
// by how that FG% compares with the league in the same zone. Used by the game pages and the
// player pop-up (window.NBAF.court). Zones match court.py: inside 14 ft, midrange and threes
// split left / center / right, corner threes on their own.
(function () {
  const R3 = 23.75, CY = 8.95, INS = 14, CEN = 30, TOP = 30, BASE = -5.25, S = 10, PAD = 64;
  const ZONES = ["inside", "mid_left", "mid_center", "mid_right", "corner_left", "corner_right", "arc_left", "arc_center", "arc_right"];
  const NAMES = { inside: "Inside 14 ft", mid_left: "Midrange left", mid_center: "Midrange center", mid_right: "Midrange right",
    corner_left: "Left corner 3", corner_right: "Right corner 3", arc_left: "Left wing 3", arc_center: "Top of the arc 3", arc_right: "Right wing 3" };
  const MIN_SHOTS = 8; // fewer attempts than this: no color, no percentage
  const rad = (d) => (d * Math.PI) / 180;
  const P = (r, deg) => [25 + r * Math.sin(rad(deg)), r * Math.cos(rad(deg))];
  const arc = (r, a, b, n = 28) => Array.from({ length: n + 1 }, (_, i) => P(r, a + ((b - a) * i) / n));
  const BRK = (Math.asin(22 / R3) * 180) / Math.PI; // where the arc meets the corner line
  const IN_B = (Math.acos(BASE / INS) * 180) / Math.PI; // where the 14-ft circle meets the baseline
  const top = (deg) => [25 + TOP * Math.tan(rad(deg)), TOP];
  const mirror = (pts) => pts.map(([x, y]) => [50 - x, y]);
  const midLeft = [...arc(R3, -CEN, -BRK), [3, BASE], [25 - Math.sqrt(INS * INS - BASE * BASE), BASE], ...arc(INS, -IN_B, -CEN)];
  const arcLeft = [top(-CEN), [0, TOP], [0, CY], [3, CY], ...arc(R3, -BRK, -CEN)];
  const SHAPES = {
    inside: arc(INS, -IN_B, IN_B),
    mid_center: [...arc(INS, -CEN, CEN), ...arc(R3, CEN, -CEN)],
    mid_left: midLeft, mid_right: mirror(midLeft),
    corner_left: [[0, BASE], [3, BASE], [3, CY], [0, CY]], corner_right: mirror([[0, BASE], [3, BASE], [3, CY], [0, CY]]),
    arc_center: [...arc(R3, -CEN, CEN), top(CEN), top(-CEN)],
    arc_left: arcLeft, arc_right: mirror(arcLeft),
  };
  const LABEL = { inside: [25, 7.2], mid_center: [25, 18.6], mid_left: [8.4, 8.4], mid_right: [41.6, 8.4],
    arc_center: [25, 26.6], arc_left: [5.6, 21], arc_right: [44.4, 21], corner_left: [-3.3, 1.2], corner_right: [53.3, 1.2] };
  const X = (x) => x * S + PAD, Y = (y) => (y - BASE) * S;
  const pts = (list) => list.map(([x, y]) => `${X(x).toFixed(1)},${Y(y).toFixed(1)}`).join(" ");
  const W = 50 * S + 2 * PAD, H = (TOP - BASE) * S;
  const LINES = `
    <path d="M${X(0)} ${Y(TOP)} V${Y(BASE)} H${X(50)} V${Y(TOP)}"/>
    <rect x="${X(17)}" y="${Y(BASE)}" width="${16 * S}" height="${19 * S}"/>
    <path d="M${X(19)} ${Y(13.75)} A${6 * S} ${6 * S} 0 0 0 ${X(31)} ${Y(13.75)}"/>
    <path d="M${X(21)} ${Y(0)} A${4 * S} ${4 * S} 0 0 0 ${X(29)} ${Y(0)}"/>
    <path d="M${X(22)} ${Y(-1.25)} H${X(28)}"/>
    <circle cx="${X(25)}" cy="${Y(0)}" r="${0.75 * S}"/>
    <path d="M${X(3)} ${Y(BASE)} V${Y(CY)} A${R3 * S} ${R3 * S} 0 0 0 ${X(47)} ${Y(CY)} V${Y(BASE)}"/>`;

  const total = (zones) => ZONES.reduce((a, z) => a + zones[z][0], 0);
  const pct = (v) => (v == null ? "–" : Math.round(v * 100) + "%");

  // zones / league: {zone: [fga, fgm]}. Returns an <svg> string.
  function court(zones, league, label) {
    const n = total(zones), ln = total(league);
    const cells = ZONES.map((z) => {
      const [fga, fgm] = zones[z], [lfga, lfgm] = league[z];
      const fg = fga >= MIN_SHOTS ? fgm / fga : null, lfg = lfga ? lfgm / lfga : null;
      const share = n ? fga / n : 0, lshare = ln ? lfga / ln : 0;
      const t = fg == null || lfg == null ? 0 : Math.max(-1, Math.min(1, (fg - lfg) / 0.08));
      const fill = fg == null ? "var(--line)" : t >= 0 ? "var(--diverge-neg)" : "var(--diverge-pos)";
      const op = fg == null ? 0.5 : 0.1 + 0.55 * Math.abs(t);
      const more = lshare && share > lshare * 1.15 ? " ▲" : lshare && share < lshare * 0.85 && fga ? " ▼" : "";
      return { z, fga, fg, lfg, share, lshare, fill, op, more };
    });
    const desc = cells.map((c) => `${NAMES[c.z]}: ${pct(c.share)} of shots, ${c.fg == null ? "too few to rate" : pct(c.fg) + " made (league " + pct(c.lfg) + ")"}`).join("; ");
    const shapes = cells.map((c) => `<polygon class="zn" points="${pts(SHAPES[c.z])}" fill="${c.fill}" fill-opacity="${c.op.toFixed(2)}"><title>${NAMES[c.z]}: ${c.fga} shots, ${c.fg == null ? "too few to rate" : pct(c.fg) + " made, league " + pct(c.lfg)}</title></polygon>`).join("");
    const labels = cells.map((c) => {
      const [x, y] = LABEL[c.z];
      return `<text x="${X(x)}" y="${Y(y)}" class="fg">${c.fg == null ? "–" : pct(c.fg)}</text>
        <text x="${X(x)}" y="${Y(y) + 21}" class="fq">${pct(c.share)}${c.more}</text>`;
    }).join("");
    return `<svg class="court-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label ? label + ". " : ""}${desc}">
      <g>${shapes}</g><g class="lines">${LINES}</g><g class="labels">${labels}</g></svg>`;
  }

  function summary(zones) {
    const n = total(zones);
    if (!n) return "";
    const made = ZONES.reduce((a, z) => a + zones[z][1], 0);
    const threes = ZONES.filter((z) => z.startsWith("arc") || z.startsWith("corner")).reduce((a, z) => a + zones[z][1], 0);
    const att3 = ZONES.filter((z) => z.startsWith("arc") || z.startsWith("corner")).reduce((a, z) => a + zones[z][0], 0);
    return `${n.toLocaleString()} shots · ${pct(made / n)} made · eFG ${pct((made + 0.5 * threes) / n)} · ${pct(att3 / n)} from three`;
  }

  const legend = `<div class="court-legend"><span><i class="sw hot"></i>Hotter than league</span><span><i class="sw cold"></i>Colder</span>
    <span>Big number: FG% · small: share of shots</span><span>▲▼ more / fewer shots from there than average</span></div>`;

  window.NBAF = Object.assign(window.NBAF || {}, { court, courtSummary: summary, courtLegend: legend });

  // Game pages: one chart per team, with "their shots / shots allowed" and season switches.
  const el = document.getElementById("court-data");
  if (!el) return;
  const D = JSON.parse(el.textContent);
  if (!D.seasons.length) return;
  let season = D.seasons[D.seasons.length - 1];
  const thin = Object.values(D.teams).some((t) => !t[season] || !t[season].off || total(t[season].off) < 300);
  if (thin && D.seasons.length > 1) season = D.seasons[0]; // early in the season: start from last season
  const mode = {};
  const boxes = [...document.querySelectorAll("[data-court-team]")];
  function draw() {
    document.querySelectorAll("[data-season]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.season === season));
    boxes.forEach((box) => {
      const tid = box.dataset.courtTeam, m = mode[tid] || "off";
      box.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === m));
      const zones = (D.teams[tid][season] || {})[m];
      const out = box.querySelector(".court-box");
      out.innerHTML = zones
        ? court(zones, D.league[season], `${box.dataset.name}, ${m === "off" ? "their shots" : "shots they allow"}, ${season}`) + `<p class="court-sum">${summary(zones)}</p>`
        : `<p class="empty">No shot locations for ${season} yet.</p>`;
    });
  }
  document.addEventListener("click", (e) => {
    const s = e.target.closest("[data-season]");
    if (s) { season = s.dataset.season; draw(); return; }
    const b = e.target.closest("[data-mode]");
    if (b) { mode[b.closest("[data-court-team]").dataset.courtTeam] = b.dataset.mode; draw(); }
  });
  draw();
})();
