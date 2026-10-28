// Cold Case: a daily NBA whodunit. Today's file is picked from the Central-time date (the build
// publishes through tomorrow's, so it switches at midnight); ?case=N opens an earlier one.
// Progress is kept per case in localStorage. The answer and the alibis sit in `sealed` (base64
// JSON): not secret, just not in plain sight.
(function () {
  const root = document.getElementById("cold");
  if (!root) return;
  const INDEX = JSON.parse(document.getElementById("cc-index").textContent);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const RD = '<span class="rd" aria-label="redacted"></span>';
  const pad = (n) => String(n).padStart(3, "0");
  const QUARTER = { 1: "1st", 2: "2nd", 3: "3rd", 4: "4th" };
  const POS = { G: "Guard", F: "Forward", C: "Center" };
  const RANKS = [[0, "Psychic"], [1, "Hall of Fame detective"], [2, "Lieutenant"], [3, "Detective"], [5, "Beat cop"], [7, "Mall security"], [99, "Desk duty"]];

  const centralToday = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Chicago" }).format(new Date());
  const byDate = Math.floor((Date.parse(centralToday) - Date.parse(root.dataset.start)) / 864e5) + 1;
  const latest = Math.max(1, Math.min(byDate, INDEX.length));
  const asked = parseInt(new URLSearchParams(location.search).get("case"), 10);
  const n = asked >= 1 && asked <= latest ? asked : latest;
  const key = `coldcase:${n}`;
  const load = (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } };
  let st = { clues: 0, wrong: [], crossed: [], solved: false, ...(load(key) || {}) };
  const save = () => { try { localStorage.setItem(key, JSON.stringify(st)); } catch (e) { /* private mode */ } };

  document.getElementById("cc-archive").innerHTML = INDEX.slice(0, latest).reverse().map((c) =>
    `<a href="?case=${c.n}" class="${c.n === n ? "on" : ""}${(load(`coldcase:${c.n}`) || {}).solved ? " done" : ""}" title="${esc(c.title)} · ${c.difficulty}">#${pad(c.n)}</a>`).join("");

  fetch(`coldcase/case-${pad(n)}.json`).then((r) => r.json()).then(start).catch(() => {
    document.getElementById("hero-h").textContent = "Case file missing";
  });

  function start(C) {
    const S = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(C.sealed), (c) => c.charCodeAt(0))));
    const T = C.team, set = C.setup;
    const rank = () => RANKS.find(([k]) => st.clues + 2 * st.wrong.length <= k)[1];

    // ---- hero ------------------------------------------------------------------------------
    document.getElementById("cc-no").textContent = `File #${pad(n)}`;
    const diff = document.getElementById("cc-diff");
    diff.hidden = false;
    diff.textContent = C.difficulty;
    document.getElementById("hero-h").textContent = C.title;
    const q = QUARTER[set.period] ? `${QUARTER[set.period]} quarter` : "Overtime";
    const standing = set.before < 0 ? `trailed ${RD} by <strong>${-set.before}</strong>`
      : set.before > 0 ? `led ${RD} by <strong>${set.before}</strong>` : `were tied with ${RD}`;
    const dek = document.getElementById("cc-dek");
    dek.innerHTML = `${q}, <strong>${esc(set.clock)}</strong> left, some night in the ${esc(set.season)} season. The
      <strong>${esc(T.name)}</strong> ${standing}. ${set.run[0]}–${set.run[1]} later, they led by <strong>${set.after}</strong>.
      Somebody did this. Six suspects, one culprit: find who broke the game open, with as few clues as you can.`;

    // ---- exhibit A: the scene --------------------------------------------------------------
    const red = (t) => esc(t).replace(/█+/g, RD);
    document.getElementById("cc-log").innerHTML = C.scene.map((s) => `
      <li class="${s.us ? "us" : ""}${s.made ? "" : " miss"}"><span class="t">${esc(s.clock)}</span><span>${red(s.text)}</span>
        <span class="sc">${esc(T.abbr)} <b>${s.score[0]}</b> · <span class="opp">${RD}</span> ${s.score[1]}</span></li>`).join("");
    const margins = [set.before, ...C.scene.map((s) => s.score[0] - s.score[1])];
    const lo = Math.min(...margins, -1), hi = Math.max(...margins, 1);
    const y = (m) => 64 - ((m - lo) / (hi - lo)) * 58;
    document.getElementById("cc-flow").innerHTML = `<line x1="0" x2="600" y1="${y(0)}" y2="${y(0)}" stroke="currentColor" stroke-opacity=".25" stroke-dasharray="4 4"/>
      <polyline points="${margins.map((m, i) => `${(i / (margins.length - 1)) * 600},${y(m).toFixed(1)}`).join(" ")}" fill="none" stroke="var(--stamp)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
      <text x="4" y="${y(0) - 4}" font-size="11" fill="currentColor" opacity=".55">tied</text>`;

    // ---- exhibit B: the suspects -----------------------------------------------------------
    const line = (s) => s ? `<b>${s.pts}</b> pts · <b>${s.reb}</b> reb · <b>${s.ast}</b> ast · <b>${s.stl}</b> stl · <b>${s.blk}</b> blk <span>(${s.g} games)</span>` : "No season line on file.";
    const mix = (m) => m ? `<div class="cc-mix">${[["inside", "Inside"], ["mid", "Midrange"], ["three", "Three"]].map(([z, l]) =>
      `<div><div class="bar"><span style="height:${Math.round(m[z] * 100)}%"></span></div>${l} ${Math.round(m[z] * 100)}%</div>`).join("")}</div>` : `<div class="meta">No shot profile on file.</div>`;
    const sus = document.getElementById("cc-sus");
    sus.innerHTML = C.suspects.map((p) => `
      <article class="cc-sus" data-id="${p.id}">
        <span class="cc-stamp cleared">Cleared</span><span class="cc-stamp guilty">Guilty</span>
        <div class="top"><span class="cc-mug"><img src="https://a.espncdn.com/combiner/i?img=/i/headshots/nba/players/full/${p.id}.png&w=96&h=70" alt="" onerror="this.remove()"><span class="no">#${esc(p.jersey)}</span></span>
          <div><div class="nm">${esc(p.name)}</div><div class="meta">${POS[p.pos] || esc(p.pos)} · ${esc(p.ht)} · ${p.starter ? "starter" : "bench"}</div></div></div>
        <div class="line">That season: ${line(p.season)}</div>
        ${mix(p.shots)}
        <div class="night">That night: <span class="night-line">${RD}</span></div>
        <div class="alibi"></div>
        <div class="cc-acts"><button type="button" class="cross" data-id="${p.id}">Cross off</button><button type="button" class="accuse" data-id="${p.id}">Accuse</button></div>
      </article>`).join("");
    sus.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || st.solved) return;
      const id = b.dataset.id;
      if (b.classList.contains("cross")) st.crossed = st.crossed.includes(id) ? st.crossed.filter((x) => x !== id) : [...st.crossed, id];
      else if (id === S.culprit) st.solved = true;
      else if (!st.wrong.includes(id)) st.wrong.push(id);
      save(); render();
    });
    document.getElementById("cc-clue").addEventListener("click", () => {
      if (st.solved || st.clues >= C.clues.length) return;
      st.clues++; save(); render();
    });

    function render() {
      document.getElementById("cc-n-clues").textContent = `${st.clues} of ${C.clues.length}`;
      document.getElementById("cc-n-wrong").textContent = st.wrong.length;
      document.getElementById("cc-rank").textContent = rank();
      document.getElementById("cc-clues").innerHTML = C.clues.map((c, i) => i < st.clues
        ? `<li><span class="n">Clue ${i + 1} · ${c.kind === "witness" ? "witness" : "from the file"}</span>${esc(c.text)}</li>`
        : i === st.clues && !st.solved ? `<li class="locked"><span class="n">Clue ${i + 1}</span>Sealed. Costs one point.</li>` : "").join("");
      const btn = document.getElementById("cc-clue");
      btn.disabled = st.solved || st.clues >= C.clues.length;
      btn.textContent = st.solved ? "Case closed" : st.clues >= C.clues.length ? "No more clues" : "Request a clue";
      document.querySelectorAll(".cc-sus").forEach((el) => {
        const id = el.dataset.id, out = st.wrong.includes(id) || (st.solved && id !== S.culprit);
        el.classList.toggle("crossed", st.crossed.includes(id) && !out && !st.solved);
        el.classList.toggle("innocent", out);
        el.classList.toggle("guilty", st.solved && id === S.culprit);
        el.querySelector(".alibi").textContent = out ? S.alibis[id] || "" : "";
        el.querySelector(".cross").textContent = st.crossed.includes(id) ? "Restore" : "Cross off";
        el.querySelectorAll("button").forEach((b) => { b.disabled = st.solved || st.wrong.includes(id); });
        if (st.solved) {
          const g = S.lines[id];
          el.querySelector(".night-line").textContent = `${g.pts} pts, ${g.reb} reb, ${g.ast} ast in ${g.minutes} min`;
        }
      });
      if (st.solved) close();
    }

    function close() {
      const date = new Date(S.date + "T12:00:00").toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
      const marks = "🟨".repeat(st.clues) + "🟥".repeat(st.wrong.length) + "🟩";
      const share = `🕵️ Cold Case #${pad(n)} · ${C.title}\n${marks}\n${rank()} · ${st.clues} clue${st.clues === 1 ? "" : "s"}, ${st.wrong.length} wrong`;
      const box = document.getElementById("cc-closed");
      box.hidden = false;
      box.innerHTML = `<span class="big-stamp">Case closed</span>
        <p class="kicker" style="color:var(--stamp)">${esc(rank())}</p>
        <h3>${esc(S.name)}</h3>
        <p>${esc(date)}${S.venue ? " · " + esc(S.venue) : ""}<br>${esc(T.name)} ${S.final[0]}, ${esc(S.opp)} ${S.final[1]}</p>
        <div class="story">${esc(S.story)}</div>
        <pre class="share">${esc(share)}</pre>
        <button type="button" class="btn" id="cc-copy">Copy result</button>`;
      document.getElementById("cc-copy").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(share); document.getElementById("cc-copy").textContent = "Copied"; } catch (e) { /* no clipboard */ }
      });
      dek.querySelectorAll(".rd").forEach((r) => r.replaceWith(Object.assign(document.createElement("strong"), { textContent: `the ${S.opp}` })));
      document.querySelectorAll("#cc-log .opp").forEach((r) => { r.textContent = S.opp.split(" ").slice(-1)[0]; });
      const link = document.querySelector(`#cc-archive a[href="?case=${n}"]`);
      if (link) link.classList.add("done");
    }
    render();
  }
})();
