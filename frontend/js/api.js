/* Thin API client. Every call reports its own failure rather than throwing
   the whole page away — a dead endpoint must not blank the dashboard. */
const API = (() => {
  const base = '';
  let lastError = null;

  async function req(path, opts = {}) {
    try {
      const res = await fetch(base + path, {
        headers: { 'Accept': 'application/json', ...(opts.headers || {}) },
        ...opts,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { const j = await res.json(); detail = j.detail || j.error || detail; } catch (e) {}
        throw new Error(`${res.status} ${detail}`);
      }
      return await res.json();
    } catch (err) {
      lastError = { path, message: err.message, at: new Date().toISOString() };
      console.warn('[API]', path, err.message);
      throw err;
    }
  }
  const get = (p) => req(p);
  const post = (p, body) => req(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const put = (p, body) => req(p, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const del = (p) => req(p, { method: 'DELETE' });

  /**
   * Fire a map of requests and hand each result to its own handler the moment
   * it lands. A management screen should paint the cheap panels immediately
   * rather than waiting on the slowest one; a failed panel reports itself and
   * leaves the rest of the screen intact.
   */
  function progressive(map, handlers, onDone) {
    const keys = Object.keys(map);
    let left = keys.length;
    keys.forEach(k => {
      Promise.resolve(map[k]).then(
        v => { try { handlers[k]?.(v); } catch (e) { console.warn('[render]', k, e); } },
        e => { try { handlers.__error?.(k, e); } catch (_) {} }
      ).finally(() => { if (--left === 0) onDone?.(); });
    });
  }

  /** Resolve a batch, keeping partial results when some calls fail. */
  async function all(map) {
    const keys = Object.keys(map);
    const settled = await Promise.allSettled(keys.map(k => map[k]));
    const out = {}, failed = [];
    settled.forEach((s, i) => {
      if (s.status === 'fulfilled') out[keys[i]] = s.value;
      else { out[keys[i]] = null; failed.push({ key: keys[i], error: s.reason?.message }); }
    });
    out.__failed = failed;
    return out;
  }

  return {
    get, post, put, del, all, progressive, lastError: () => lastError,
    health: () => get('/api/health'),
    config: () => get('/api/config'),
    stats: () => get('/api/stats'),
    masters: () => get('/api/market/masters'),
    kpis: () => get('/api/market/kpis'),
    live: () => get('/api/market/live'),
    fx: () => get('/api/market/fx'),
    quality: () => get('/api/market/quality'),
    series: (q) => get('/api/market/series?' + new URLSearchParams(q)),
    compare: (c) => get('/api/market/providers/compare?commodity=' + c),
    parity: () => get('/api/market/copper/parity'),
    alStats: () => get('/api/market/aluminium/stats'),
    forecast: (c, p, pr) => get('/api/forecast?' + new URLSearchParams(
      Object.fromEntries(Object.entries({ commodity: c, provider: p, product: pr })
        .filter(([, v]) => v)))),
    leaderboard: (c) => get('/api/forecast/leaderboard?commodity=' + c),
    volatility: () => get('/api/forecast/volatility'),
    trend: (c) => get('/api/forecast/trend?commodity=' + c),
    rollup: () => get('/api/bom/rollup'),
    transformers: () => get('/api/bom/transformers'),
    transformer: (j) => get('/api/bom/transformers/' + encodeURIComponent(j)),
    impact: (body) => post('/api/bom/impact', body || {}),
    waterfall: (j) => get('/api/bom/waterfall' + (j ? '?job_no=' + encodeURIComponent(j) : '')),
    sensitivity: (c) => get('/api/bom/sensitivity?commodity=' + c),
    whatIf: (b) => post('/api/bom/what-if', b),
    coverage: () => get('/api/bom/coverage'),
    monthly: () => get('/api/bom/exposure/monthly'),
    matrix: () => get('/api/bom/exposure/matrix'),
    flow: (c, j) => get('/api/bom/flow?commodity=' + c + (j ? '&job_no=' + encodeURIComponent(j) : '')),
    costBacktest: (j) => get('/api/bom/backtest' + (j ? '?job_no=' + encodeURIComponent(j) : '')),
    recommendation: (c) => get('/api/procurement/recommendation?commodity=' + c),
    recommendations: () => get('/api/procurement/recommendations'),
    priceLock: (b) => post('/api/procurement/price-lock', b),
    summary: () => get('/api/procurement/summary'),
    executive: () => get('/api/procurement/executive'),
    procKpis: () => get('/api/procurement/kpis'),
    alerts: () => get('/api/procurement/alerts'),
    evaluateAlerts: () => post('/api/procurement/alerts/evaluate'),
    ackAlert: (id) => post(`/api/procurement/alerts/${id}/ack`),
    quotes: () => get('/api/quotes'),
    createQuote: (b) => post('/api/quotes', b),
    deleteQuote: (id) => del('/api/quotes/' + id),
    settings: () => get('/api/data/settings'),
    saveSetting: (b) => put('/api/data/settings', b),
    sourceLog: () => get('/api/data/source-log'),
    refresh: (full) => post('/api/data/refresh' + (full ? '?full=true' : '')),
    scheduler: () => get('/api/scheduler'),
  };
})();
