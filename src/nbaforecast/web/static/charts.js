// Renders every [data-chart] element from the Vega-Lite spec embedded in the page.
// Specs use color tokens ("@series-1") that resolve to this page's CSS custom properties, so
// light and dark mode each use their own validated steps. Charts re-render on theme change.
(function () {
  const TOKENS = {
    "@series-1": "--series-1",
    "@series-2": "--series-2",
    "@diverge-pos": "--diverge-pos",
    "@diverge-neg": "--diverge-neg",
    "@surface": "--surface-1",
    "@ink-muted": "--ink-muted",
    "@ink-secondary": "--ink-secondary",
  };

  function readTokens() {
    const style = getComputedStyle(document.documentElement);
    const out = {};
    for (const [token, prop] of Object.entries(TOKENS)) out[token] = style.getPropertyValue(prop).trim();
    out.grid = style.getPropertyValue("--grid").trim();
    out.baseline = style.getPropertyValue("--baseline").trim();
    return out;
  }

  function resolve(node, t) {
    if (typeof node === "string") return t[node] ?? node;
    if (Array.isArray(node)) return node.map((n) => resolve(n, t));
    if (node && typeof node === "object") {
      const out = {};
      for (const key of Object.keys(node)) out[key] = resolve(node[key], t);
      return out;
    }
    return node;
  }

  function config(t) {
    const font = 'system-ui, -apple-system, "Segoe UI", sans-serif';
    return {
      background: null,
      font,
      view: { stroke: null },
      padding: { left: 4, right: 12, top: 6, bottom: 4 },
      axis: {
        domainColor: t.baseline,
        gridColor: t.grid,
        gridWidth: 1,
        tickColor: t.baseline,
        labelColor: t["@ink-muted"],
        titleColor: t["@ink-secondary"],
        labelFontSize: 11,
        titleFontSize: 12,
        titleFontWeight: "normal",
        titlePadding: 10,
      },
      axisX: { grid: false },
      legend: {
        labelColor: t["@ink-secondary"],
        labelFontSize: 12,
        symbolType: "circle",
        symbolSize: 90,
        symbolStrokeWidth: 0,
      },
      text: { font },
    };
  }

  function renderAll() {
    if (typeof vegaEmbed !== "function") return;
    const t = readTokens();
    document.querySelectorAll("[data-chart]").forEach((el) => {
      const source = document.getElementById(el.dataset.chart);
      if (!source) return;
      const spec = resolve(JSON.parse(source.textContent), t);
      vegaEmbed(el, spec, { actions: false, renderer: "svg", config: config(t) }).catch((err) =>
        console.error("chart failed", el.dataset.chart, err),
      );
    });
  }

  window.addEventListener("DOMContentLoaded", renderAll);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderAll);
  new MutationObserver(renderAll).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
})();
