// The Rotation: a daily coaching puzzle. Hand out 240 minutes to tonight's biggest underdog.
// Each player's value per minute is the model's production rating; past their usual minutes they
// tire (value per extra minute falls, then goes negative). Day-to-day players might sit: if one
// does, their minutes are spread over everyone else, tired legs included. The win probability is
// the team + player model's own sensitivity to production. Par is the best rotation the search
// below can find; you're scored on how much of that swing you capture.
(function () {
  const el = document.getElementById("rotation-data");
  if (!el) return;
  const P = JSON.parse(el.textContent);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  P.players.forEach((p) => { p.name = esc(p.name); p.short = esc(p.short); p.position = esc(p.position); });
  const TOTAL = 240, CAP = P.max_minutes, TIRE = 12; // minutes past "usual" until a player is spent

  // --- the physics ---------------------------------------------------------------------
  // Every minute up to a player's usual is worth a full minute; the k-th minute past it is worth
  // (1 - k/TIRE) of one, so the 12th extra minute is worth nothing. Resting a player costs only
  // the minutes they don't play.
  const extraValue = (k) => k * (1 - (k + 1) / (2 * TIRE)); // sum of (1 - j/TIRE), j = 1..k
  const effMinutes = (p, m) => {
    const u = Math.round(p.usual);
    return m <= u ? m : u + extraValue(m - u);
  };
  const fresh = (p, m) => (m <= Math.round(p.usual) ? 1 : Math.max(0, 1 - (m - Math.round(p.usual)) / TIRE));
  const value = (p, m) => p.prod * effMinutes(p, m);
  function teamValue(mins) { return P.players.reduce((a, p, i) => a + value(p, mins[i]), 0); }
  // If player j sits, their minutes go to the others in proportion to what they already play.
  function without(mins, j) {
    const rest = mins.reduce((a, m, i) => a + (i === j ? 0 : m), 0);
    return mins.map((m, i) => (i === j ? 0 : rest ? Math.min(CAP, m + (mins[j] * m) / rest) : m));
  }
  // Expected team value over every game-time decision (each independent).
  function expected(mins) {
    const gtd = P.players.map((p, i) => [i, p.p_play]).filter(([, q]) => q < 1);
    let total = 0;
    for (let mask = 0; mask < 1 << gtd.length; mask++) {
      let prob = 1, m = mins.slice();
      gtd.forEach(([i, q], b) => {
        if (mask & (1 << b)) prob *= q; else { prob *= 1 - q; m = without(m, i); }
      });
      total += prob * teamValue(m);
    }
    return total;
  }
  const usual = (() => { // the coach's normal rotation, scaled to 240
    const s = P.players.reduce((a, p) => a + p.usual, 0);
    return P.players.map((p) => Math.min(CAP, Math.round((p.usual * TOTAL) / s)));
  })();
  fix(usual);
  const BASE = expected(usual);
  const winProb = (v) => 1 / (1 + Math.exp(-(P.logit_dog + P.beta * (v - BASE))));

  // Par: start from the usual rotation and keep moving single minutes to wherever they're worth
  // more (expected value), until no move helps. Deterministic, so everyone gets the same par.
  function solve() {
    const m = usual.slice();
    let best = expected(m), improved = true, guard = 0;
    while (improved && guard++ < 4000) {
      improved = false;
      let move = null;
      for (let i = 0; i < m.length; i++) for (let j = 0; j < m.length; j++) {
        if (i === j || m[i] >= CAP || m[j] <= 0) continue;
        m[i]++; m[j]--;
        const v = expected(m);
        if (v > best + 1e-9) { best = v; move = [i, j]; }
        m[i]--; m[j]++;
      }
      if (move) { m[move[0]]++; m[move[1]]--; improved = true; }
    }
    return m;
  }
  function fix(m) { // make the total exactly 240 by nudging the biggest minutes
    let diff = TOTAL - m.reduce((a, b) => a + b, 0);
    while (diff !== 0) {
      const order = m.map((v, i) => i).sort((a, b) => m[b] - m[a]);
      for (const i of order) {
        if (diff > 0 && m[i] < CAP) { m[i]++; diff--; break; }
        if (diff < 0 && m[i] > 0) { m[i]--; diff++; break; }
      }
    }
  }
  const PAR = solve();
  const PAR_P = winProb(expected(PAR));
  const BASE_P = winProb(BASE);

  // --- the board -----------------------------------------------------------------------
  const key = `rotation:${P.date}`;
  let mins = usual.slice();
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { /* private mode */ }

  const list = document.getElementById("rot-players");
  const pctEl = document.getElementById("rot-pct");
  const leftEl = document.getElementById("rot-left");
  const deltaEl = document.getElementById("rot-delta");
  const lockBtn = document.getElementById("rot-lock");
  const result = document.getElementById("rot-result");
  const meter = document.getElementById("rot-meter");

  list.innerHTML = P.players.map((p, i) => `
    <li class="rp" style="--i:${i}">
      <span class="avatar"><span class="ini">${p.short.slice(0, 2)}</span>${p.headshot ? `<img src="${p.headshot}" alt="" onerror="this.remove()">` : ""}</span>
      <span class="who"><b>${p.name}</b><span class="sub">${p.position || "–"} · usually ${Math.round(p.usual)} min · ${(p.prod * 36).toFixed(1)} per 36
        ${p.p_play < 1 ? `<span class="tag gtd" title="Day-to-day: ${Math.round(p.p_play * 100)}% chance to play">GTD ${Math.round(p.p_play * 100)}%</span>` : ""}</span></span>
      <span class="ctl">
        <button type="button" class="step" data-i="${i}" data-d="-1" aria-label="One minute less for ${p.name}">−</button>
        <input type="range" min="0" max="${CAP}" step="1" value="${mins[i]}" data-i="${i}" aria-label="Minutes for ${p.name}">
        <button type="button" class="step" data-i="${i}" data-d="1" aria-label="One minute more for ${p.name}">+</button>
        <output class="m" id="m${i}">${mins[i]}</output>
      </span>
      <span class="legs" title="Freshness"><span class="fill" id="f${i}"></span></span>
    </li>`).join("");

  function update() {
    const used = mins.reduce((a, b) => a + b, 0);
    const p = winProb(expected(mins));
    pctEl.textContent = (p * 100).toFixed(1) + "%";
    const d = (p - BASE_P) * 100;
    deltaEl.textContent = `${d >= 0 ? "+" : ""}${d.toFixed(1)} pts vs. the usual rotation`;
    leftEl.textContent = TOTAL - used === 0 ? "All 240 minutes handed out" : `${TOTAL - used > 0 ? TOTAL - used + " minutes left" : used - TOTAL + " minutes over"}`;
    leftEl.className = "left" + (used === TOTAL ? " ok" : " warn");
    lockBtn.disabled = used !== TOTAL;
    const found = PAR_P > BASE_P ? Math.max(0, Math.min(1, (p - BASE_P) / (PAR_P - BASE_P))) : 1;
    meter.style.setProperty("--p", (found * 100).toFixed(1));
    document.getElementById("rot-found").textContent = `${Math.round(found * 100)}% of par's swing found`;
    P.players.forEach((pl, i) => {
      const f = fresh(pl, mins[i]); // value of their next minute, as a share of a fresh one
      const bar = document.getElementById("f" + i);
      bar.style.width = (f * 100).toFixed(0) + "%";
      bar.className = "fill " + (f > 0.9 ? "ok" : f > 0.6 ? "meh" : "bad");
      document.getElementById("m" + i).textContent = mins[i];
      list.querySelector(`input[data-i="${i}"]`).value = mins[i];
    });
  }
  list.addEventListener("input", (e) => {
    if (e.target.matches('input[type="range"]')) { mins[+e.target.dataset.i] = +e.target.value; update(); }
  });
  list.addEventListener("click", (e) => {
    const b = e.target.closest(".step");
    if (!b) return;
    const i = +b.dataset.i;
    mins[i] = Math.max(0, Math.min(CAP, mins[i] + +b.dataset.d));
    update();
  });
  document.getElementById("rot-reset").addEventListener("click", () => { mins = usual.slice(); update(); });

  function grade(share, p) {
    if (Math.abs(p - BASE_P) < 0.0005) return ["Status quo", "That's the usual rotation: no harm done, no edge found."];
    if (p < BASE_P) return ["Fired", "Worse than the usual rotation. Tired legs or the wrong players got the minutes."];
    if (share >= 0.99) return ["Perfect rotation", "Par. You found the best rotation there is."];
    if (share >= 0.9) return ["Coach of the Year", "Within a whisker of par."];
    if (share >= 0.7) return ["Playoff coach", "Most of the swing; a few minutes in the wrong place."];
    if (share >= 0.4) return ["Hot seat", "Some of the swing, but the legs or the dice got you."];
    return ["Assistant coach", "A small edge. More minutes for the most productive players, until they tire."];
  }
  function finish(m) {
    const p = winProb(expected(m));
    const swing = PAR_P - BASE_P;
    const share = swing > 0 ? Math.max(0, (p - BASE_P) / swing) : 1;
    const [title, line] = grade(share, p);
    const blocks = "🟧".repeat(Math.round(share * 5)) + "⬛".repeat(5 - Math.round(share * 5));
    const share_text = `🏀 The Rotation · ${P.date}\n${P.underdog.abbr} vs ${P.favorite.abbr}: ${(BASE_P * 100).toFixed(1)}% → ${(p * 100).toFixed(1)}% (par ${(PAR_P * 100).toFixed(1)}%)\n${blocks} ${Math.round(share * 100)}% of the swing · ${title}`;
    result.hidden = false;
    result.innerHTML = `
      <p class="kicker">${title}</p>
      <h3 class="display">${Math.round(share * 100)}% of the possible swing</h3>
      <p>${line} Your rotation: <b>${(p * 100).toFixed(1)}%</b>. Usual rotation: ${(BASE_P * 100).toFixed(1)}%. Par: <b>${(PAR_P * 100).toFixed(1)}%</b>.</p>
      <details><summary>See the par rotation</summary>
        <ul class="par">${P.players.map((pl, i) => `<li><span>${pl.name}</span><span>${PAR[i]} min${PAR[i] !== m[i] ? ` <span class="muted">(you: ${m[i]})</span>` : ""}</span></li>`).join("")}</ul>
      </details>
      <pre class="share" id="rot-share">${share_text}</pre>
      <button type="button" id="rot-copy" class="btn">Copy result</button>`;
    document.getElementById("rot-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(share_text); document.getElementById("rot-copy").textContent = "Copied"; } catch (e) { /* no clipboard */ }
    });
    try { localStorage.setItem(key, JSON.stringify({ mins: m, share })); } catch (e) { /* private mode */ }
  }
  lockBtn.addEventListener("click", () => finish(mins.slice()));
  if (saved && Array.isArray(saved.mins) && saved.mins.length === mins.length) { mins = saved.mins; finish(mins.slice()); }
  update();
})();
