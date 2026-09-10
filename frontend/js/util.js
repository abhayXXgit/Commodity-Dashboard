/* Formatting + small DOM helpers. No framework, no build step. */
const U = (() => {
  const nf = (d = 0) => new Intl.NumberFormat('en-IN', {
    minimumFractionDigits: d, maximumFractionDigits: d });

  /** Indian compact currency: Cr / L / K. */
  function inr(v, dec = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const a = Math.abs(v), s = v < 0 ? '-' : '';
    if (a >= 1e7) return `${s}₹${nf(dec).format(a / 1e7)} Cr`;
    if (a >= 1e5) return `${s}₹${nf(dec).format(a / 1e5)} L`;
    if (a >= 1e3) return `${s}₹${nf(1).format(a / 1e3)} K`;
    return `${s}₹${nf(0).format(a)}`;
  }
  /** Full rupee figure, e.g. 8,94,200 */
  function rs(v, d = 0) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return '₹' + nf(d).format(v);
  }
  function num(v, d = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return nf(d).format(v);
  }
  function pct(v, d = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return `${v >= 0 ? '+' : ''}${nf(d).format(v)}%`;
  }
  /** Procurement semantics: a RISING price is adverse (red). */
  function dirClass(v) {
    if (v === null || v === undefined || Number.isNaN(v) || Math.abs(v) < 0.005) return 'flat';
    return v > 0 ? 'up' : 'down';
  }
  function chg(v, d = 2) {
    const c = dirClass(v);
    const arrow = c === 'up' ? '▲' : c === 'down' ? '▼' : '■';
    return `<span class="chg ${c}">${arrow} ${pct(v, d)}</span>`;
  }
  function signalClass(sig) {
    const s = (sig || '').toUpperCase();
    if (s === 'BUY NOW') return 'buy';
    if (s === 'BUY PARTIAL') return 'partial';
    if (s === 'PHASED PROCUREMENT') return 'phased';
    if (s === 'LOCK PRICE') return 'lock';
    if (s === 'DO NOT LOCK') return 'nolock';
    if (s === 'NEGOTIATE') return 'negotiate';
    return 'monitor';
  }
  function dataClassTag(dc) {
    const map = {
      LIVE: ['good', 'Live'], VERIFIED_HISTORICAL: ['info', 'Verified'],
      SUPPLIER_QUOTE: ['info', 'Quote'], ESTIMATED: ['warn', 'Estimated'],
      FORECAST: ['warn', 'Forecast'], DEMO_DATA: ['warn', 'Demo data'],
      DERIVED: ['flat', 'Derived'],
    };
    const [cls, label] = map[dc] || ['flat', dc || '—'];
    return `<span class="tag ${cls}">${label}</span>`;
  }
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function el(id) { return document.getElementById(id); }
  function html(id, s) { const n = el(id); if (n) n.innerHTML = s; }
  function ago(iso) {
    if (!iso) return '—';
    const d = new Date(iso), mins = (Date.now() - d.getTime()) / 60000;
    if (mins < 1) return 'just now';
    if (mins < 60) return `${Math.round(mins)} min ago`;
    if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
    return `${Math.round(mins / 1440)} d ago`;
  }
  function date(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('en-IN',
      { day: '2-digit', month: 'short', year: 'numeric' });
  }
  function toast(msg, kind = '') {
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }
  /** Build a table from rows + column defs. */
  function table(cols, rows, opts = {}) {
    if (!rows || !rows.length) return `<div class="chart-empty">${opts.empty || 'No data'}</div>`;
    const head = cols.map(c => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('');
    const body = rows.map(r => {
      const cls = opts.rowClass ? opts.rowClass(r) : '';
      const tds = cols.map(c => {
        const v = c.render ? c.render(r) : r[c.key];
        return `<td class="${c.num ? 'num' : ''} ${c.cls || ''}">${v ?? '—'}</td>`;
      }).join('');
      return `<tr class="${cls}">${tds}</tr>`;
    }).join('');
    return `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  return { inr, rs, num, pct, chg, dirClass, signalClass, dataClassTag, esc,
           el, html, ago, date, toast, table };
})();
