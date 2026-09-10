/* =====================================================================
   Standalone controller. Reads the embedded snapshot and computes
   everything (what-if, filtering, aggregation) in the browser.
   No network calls are made — and none are possible.
   ===================================================================== */
const SA = (() => {
  const D = JSON.parse(new TextDecoder().decode(
    Uint8Array.from(atob(document.getElementById('snapshot').textContent.trim()),
      c => c.charCodeAt(0))));
  const state = { page: 'dashboard', histKey: null, histDays: 365, fcKey: null, bomC: 'CU' };
  const seriesByKey = Object.fromEntries(D.series.map(s => [s.key, s]));

  /* ------------------------------------------------------------- chrome */
  function banner() {
    const demo = D.data_mode === 'DEMO' ||
      D.series.some(s => s.data_class === 'DEMO_DATA');
    const parts = [`<div class="banner warn">
      <span class="pill demo"><span class="dot"></span>Snapshot</span>
      <span><b>Offline snapshot.</b> Every figure here was captured at
      ${U.esc(new Date(D.generated_at).toLocaleString('en-IN'))} and is frozen.
      This file cannot fetch live prices.</span></div>`];
    if (demo) parts.push(`<div class="banner err">
      <span class="pill stale"><span class="dot"></span>Demo data</span>
      <span>The underlying price history is <b>simulated</b>, not market data.
      Connect a licensed feed or import official producer circulars before using
      these figures commercially.</span></div>`);
    U.html('bannerHost', parts.join(''));
  }

  function go(page) {
    state.page = page;
    document.querySelectorAll('.nav button').forEach(b =>
      b.classList.toggle('active', b.dataset.page === page));
    document.querySelectorAll('.page').forEach(p =>
      p.classList.toggle('active', p.id === 'page-' + page));
    window.scrollTo({ top: 0 });
    (renderers[page] || (() => {}))();
    setTimeout(Charts.resizeAll, 50);
  }

  /* ---------------------------------------------------------- dashboard */
  function dashboard() {
    U.html('kpiStrip', D.kpi_cards.map(c => `
      <div class="kpi ${U.dirClass(c.change_pct)}">
        <div class="k-label">${U.esc(c.label)}</div>
        <div class="k-value">${U.rs(c.current)} <span class="k-unit">/MT</span></div>
        <div class="k-row">${U.chg(c.change_pct)}
          <span class="faint mono">30d ${U.pct(c.d30, 1)}</span>
          <span class="faint mono">1y ${U.pct(c.d365, 1)}</span></div>
        <div class="k-row">
          ${c.forecast_30d_pct != null ? `<span class="tag ${c.forecast_30d_pct > 0 ? 'bad' : 'good'}">30d fc ${U.pct(c.forecast_30d_pct, 1)}</span>` : ''}
          ${U.dataClassTag(c.data_class)}</div>
        <div class="k-foot">${U.esc(c.source_currency)} ${U.num(c.source_price, 0)}/${U.esc(c.source_unit)} · ${U.esc(c.as_of)}</div>
      </div>`).join(''));

    const e = D.executive;
    U.html('execBlock', `<div class="grid g4">
      <div class="card"><div class="card-head"><h3>Total commodity exposure</h3></div>
        <div class="k-value mono" style="font-size:24px">${U.inr(e.total_exposure)}</div></div>
      <div class="card"><div class="card-head"><h3>30-day forecast exposure</h3></div>
        <div class="k-value mono" style="font-size:24px">${U.inr(e.forecast_exposure['30d'])}</div>
        <div class="k-row">${U.chg(e.total_exposure ? e.potential_exposure_30d / e.total_exposure * 100 : 0)}
          <span class="faint">${U.inr(e.potential_exposure_30d)} potential</span></div></div>
      <div class="card"><div class="card-head"><h3>Overall commodity risk</h3></div>
        <div><span class="signal ${e.overall_risk === 'HIGH' ? 'nolock' : e.overall_risk === 'MEDIUM' ? 'partial' : 'buy'}">${U.esc(e.overall_risk)}</span></div>
        <div class="k-row" style="margin-top:8px">${Object.entries(e.risk).map(([c, r]) =>
          `<span class="tag ${r.risk_band === 'HIGH' ? 'bad' : r.risk_band === 'MEDIUM' ? 'warn' : 'good'}">${c} ${r.risk_band}</span>`).join('')}</div></div>
      <div class="card"><div class="card-head"><h3>Top project at risk</h3></div>
        ${e.top_project_at_risk ? `<div class="k-value mono" style="font-size:17px">${U.esc(e.top_project_at_risk.job_no)}</div>
          <div class="faint">${U.esc(e.top_project_at_risk.customer || '')} · ${e.top_project_at_risk.rating_mva} MVA × ${e.top_project_at_risk.quantity}</div>
          <div class="k-row"><span class="tag bad">${U.esc(e.top_project_at_risk.commodity)} ${U.inr(e.top_project_at_risk.exposure)}</span></div>
          <div class="card-note">${U.esc(e.top_project_at_risk.quadrant)}</div>` : ''}</div>
    </div>`);

    U.html('signalBlock', D.recommendations.filter(r => ['CU', 'AL'].includes(r.commodity))
      .map(recoCard).join(''));

    U.html('summaryBlock', `<ul class="reason-list" style="font-size:12.5px;line-height:1.75">
      ${D.summary.sentences.map(s => `<li>${U.esc(s)}</li>`).join('')}</ul>`);

    renderAlerts('dashAlerts', D.alerts.slice(0, 6));

    U.html('impactTable', U.table([
      { label: 'Commodity', key: 'commodity', cls: 'mono' },
      { label: 'BOM MT/trf', num: true, render: r => U.num(r.bom_mt_per_transformer, 3) },
      { label: 'Total req MT', num: true, render: r => U.num(r.total_requirement_mt, 2) },
      { label: 'Current price', num: true, render: r => U.rs(r.current_price) },
      { label: 'Expected change', num: true, render: r =>
          `<span class="chg ${U.dirClass(r.price_change)}">${r.price_change >= 0 ? '+' : ''}${U.rs(r.price_change)}</span>` },
      { label: 'Impact/transformer', num: true, render: r => U.inr(r.impact_per_transformer) },
      { label: 'Total exposure', num: true, render: r => U.inr(r.total_exposure) },
      { label: 'Basis', render: r => `<span class="faint">${U.esc(r.basis)}</span>` },
    ], D.impact.rows) + `<div class="card-note">Total impact per transformer
      <b class="mono">${U.inr(D.impact.total_impact_per_transformer)}</b> · total project exposure
      <b class="mono">${U.inr(D.impact.total_project_exposure)}</b> across ${D.impact.units} transformers.</div>`);

    U.html('coverageTable', U.table([
      { label: 'Commodity', key: 'commodity', cls: 'mono' },
      { label: 'Requirement MT', num: true, render: r => U.num(r.requirement_mt, 2) },
      { label: 'Stock', num: true, render: r => U.num(r.stock_mt, 2) },
      { label: 'Open PO', num: true, render: r => U.num(r.open_po_mt, 2) },
      { label: 'Confirmed', num: true, render: r => U.num(r.confirmed_mt, 2) },
      { label: 'Uncovered MT', num: true, render: r => `<b>${U.num(r.uncovered_mt, 2)}</b>` },
      { label: 'Coverage', num: true, render: r => {
          if (!r.tracked || r.coverage_pct == null) return '<span class="tag flat">not tracked</span>';
          const p = r.coverage_pct, cls = p >= 70 ? 'good' : p >= 40 ? 'warn' : 'bad';
          return `<div style="display:flex;align-items:center;gap:6px;justify-content:flex-end">
            <div class="bar-track" style="width:56px"><div class="bar-fill ${cls}" style="width:${Math.min(p, 100)}%"></div></div>
            <span>${U.num(p, 0)}%</span></div>`; } },
      { label: 'Uncovered exposure', num: true, render: r =>
          r.uncovered_exposure == null ? '—' : U.inr(r.uncovered_exposure) },
    ], D.coverage));

    monthlyChart('chartMonthly');
  }

  function monthlyChart(id) {
    const m = D.monthly;
    if (!m?.months?.length) return Charts.empty(id, 'No delivery-dated projects');
    Charts.bars(id, m.months, m.series.filter(s => s.values.some(v => v > 0))
      .map(s => ({ name: s.commodity, data: s.values })), { stack: true, unit: 'INR exposure' });
  }

  function recoCard(r) {
    return `<div class="card">
      <div class="card-head"><h3>${r.commodity === 'CU' ? 'Copper' : r.commodity === 'AL' ? 'Aluminium' : U.esc(r.commodity)} — procurement signal</h3>
        <span class="spacer"></span>
        <span class="tag ${r.confidence === 'HIGH' ? 'good' : r.confidence === 'LOW' ? 'warn' : 'info'}">${U.esc(r.confidence)} confidence</span></div>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px">
        <span class="signal ${U.signalClass(r.signal)}">${U.esc(r.signal)}</span>
        <div class="kv" style="flex:1;min-width:180px">
          <dt>Score</dt><dd>${U.num(r.score, 1)}</dd>
          <dt>Suggested cover</dt><dd>${U.num(r.suggested_cover_pct, 0)}%</dd>
          <dt>Suggested lot</dt><dd>${U.num(r.suggested_lot_mt, 2)} MT</dd></div></div>
      <ul class="reason-list">${(r.reasons || []).slice(0, 4).map(x => `<li>${U.esc(x)}</li>`).join('')}</ul>
      <div class="card-note">${U.esc(r.disclaimer || '')}</div></div>`;
  }

  /* ------------------------------------------------------------ copper */
  function copper() {
    const s = seriesByKey['CU|BME|CU_CATHODE'];
    if (s) { priceChart('chartCuPrice', s); U.html('cuStats', statBlock(s)); }
    const p = D.parity;
    if (p?.available) {
      Charts.multiLine('chartCuParity', p.series.map(x => x.date), [
        { name: 'LME equivalent (INR)', data: p.series.map(x => x.lme_inr), color: Charts.C.blue },
        { name: 'India reference', data: p.series.map(x => x.india), color: Charts.C.amber },
      ], { zoom: true });
      U.html('cuParity', `<dl class="kv">
        <dt>LME cash</dt><dd>USD ${U.num(p.lme_usd, 2)} /MT</dd>
        <dt>USD/INR</dt><dd>${U.num(p.usdinr, 4)}</dd>
        <dt>LME equivalent</dt><dd>${U.rs(p.lme_inr_equivalent)}</dd>
        <dt>India reference</dt><dd>${U.rs(p.india_price)}</dd>
        <dt>Premium over LME</dt><dd>${U.rs(p.premium)} (${U.num(p.premium_pct, 2)}%)</dd>
        <dt>90-day avg premium</dt><dd>${U.rs(p.premium_avg_90)}</dd>
        <dt>1-year range</dt><dd>${U.rs(p.premium_min_1y)} – ${U.rs(p.premium_max_1y)}</dd>
        <dt>Basis</dt><dd><span class="tag ${p.basis_status === 'WIDE' ? 'bad' : p.basis_status === 'NARROW' ? 'good' : 'flat'}">${U.esc(p.basis_status)}</span></dd>
      </dl><div class="card-note">Indian equivalent = LME USD/MT × USD/INR.
        Premium = India reference − LME equivalent.</div>`);
    }
    const r = D.recommendations.find(x => x.commodity === 'CU');
    if (r) U.html('cuReco', recoCard(r).replace(/^<div class="card">|<\/div>$/g, ''));
  }

  /* --------------------------------------------------------- aluminium */
  function aluminium() {
    U.html('alCompare', D.compare.table.map(t => `
      <div class="card">
        <div class="card-head"><h3>${U.esc(t.product.replace('AL_', 'Aluminium ').replace('WIREROD', 'Wire Rod'))}</h3>
          <span class="spacer"></span><span class="tag ${t.spread_pct > 2 ? 'warn' : 'flat'}">spread ${U.num(t.spread_pct, 2)}%</span></div>
        ${U.table([
          { label: 'Provider', key: 'provider', cls: 'mono' },
          { label: 'Price INR/MT', num: true, render: r => U.rs(r.price) },
          { label: 'vs avg', num: true, render: r => `<span class="chg ${U.dirClass(r.vs_avg_pct)}">${U.pct(r.vs_avg_pct)}</span>` },
          { label: '30d', num: true, render: r => U.pct(r.d30, 1) },
          { label: '', render: r => r.is_lowest ? '<span class="tag good">Lowest</span>' : '' },
        ], t.entries, { rowClass: r => r.is_lowest ? 'best' : '' })}
        <div class="card-note">Lowest ${U.esc(t.lowest_provider)} ${U.rs(t.lowest)} ·
          average ${U.rs(t.average)} · spread ${U.rs(t.spread)}/MT</div></div>`).join(''));

    const prods = D.compare.table;
    Charts.bars('chartAlProviders', prods.map(t => t.product.replace('AL_', '')),
      ['NALCO', 'BALCO', 'HINDALCO'].map(pv => ({ name: pv,
        data: prods.map(t => (t.entries.find(e => e.provider === pv) || {}).price ?? 0) })),
      { unit: 'INR / MT' });

    const hist = D.compare.spread_history?.AL_INGOT;
    if (hist?.length) Charts.multiLine('chartAlSpread', hist.map(x => x.date),
      [{ name: 'Producer spread', data: hist.map(x => x.spread), color: Charts.C.violet }],
      { unit: 'INR / MT' });

    U.html('alStatsTable', U.table([
      { label: 'Provider', key: 'provider', cls: 'mono' },
      { label: 'Product', render: r => r.product.replace('AL_', '') },
      { label: 'Current', num: true, render: r => U.rs(r.current) },
      { label: '30d avg', num: true, render: r => U.rs(r.avg_30) },
      { label: '3m avg', num: true, render: r => U.rs(r.avg_90) },
      { label: '12m avg', num: true, render: r => U.rs(r.avg_365) },
      { label: 'Low', num: true, render: r => U.rs(r.min_all) },
      { label: 'High', num: true, render: r => U.rs(r.max_all) },
      { label: 'Percentile', num: true, render: r =>
          `<span class="tag ${r.percentile > 70 ? 'bad' : r.percentile < 30 ? 'good' : 'flat'}">${U.num(r.percentile, 0)}% ${U.esc(r.zone)}</span>` },
    ], D.al_stats));

    const r = D.recommendations.find(x => x.commodity === 'AL');
    if (r) U.html('alReco', recoCard(r).replace(/^<div class="card">|<\/div>$/g, ''));
  }

  /* -------------------------------------------------------- historical */
  function priceChart(id, s, days) {
    let dates = s.dates, prices = s.prices, ma = s.ma;
    if (days && days < 90000) {
      const cut = new Date(s.as_of); cut.setDate(cut.getDate() - days);
      const iso = cut.toISOString().slice(0, 10);
      const start = dates.findIndex(d => d >= iso);
      if (start > 0) {
        dates = dates.slice(start); prices = prices.slice(start);
        ma = Object.fromEntries(Object.entries(s.ma).map(([k, v]) => [k, v.slice(start)]));
      }
    }
    const points = dates.map((d, i) => {
      const p = { date: d, price: prices[i] };
      Object.entries(ma).forEach(([k, v]) => { if (v[i] != null) p[k] = v[i]; });
      return p;
    });
    Charts.priceLine(id, points, { mas: ['ma7', 'ma30', 'ma90', 'ma200'] });
    return points;
  }

  function historical() {
    const sel = U.el('histSeries');
    if (!sel.options.length) {
      sel.innerHTML = D.series.map(s => `<option value="${s.key}">${U.esc(s.label)}</option>`).join('');
      sel.onchange = () => { state.histKey = sel.value; historical(); };
      document.querySelectorAll('#periodSeg button').forEach(b => b.onclick = () => {
        document.querySelectorAll('#periodSeg button').forEach(x => x.classList.remove('active'));
        b.classList.add('active'); state.histDays = +b.dataset.days; historical();
      });
    }
    state.histKey = state.histKey || D.series[0].key;
    sel.value = state.histKey;
    const s = seriesByKey[state.histKey];
    priceChart('chartHistory', s, state.histDays);
    U.html('histStats', statBlock(s));
    Charts.multiLine('chartWeekly', s.monthly.map(m => m.date), [
      { name: 'Monthly average', data: s.monthly.map(m => m.avg), color: Charts.C.amber },
      { name: 'Monthly high', data: s.monthly.map(m => m.high), color: Charts.C.bad },
      { name: 'Monthly low', data: s.monthly.map(m => m.low), color: Charts.C.good },
    ]);
    U.html('histTable', U.table([
      { label: 'Month', key: 'date' },
      { label: 'Open', num: true, render: r => U.rs(r.open) },
      { label: 'High', num: true, render: r => U.rs(r.high) },
      { label: 'Low', num: true, render: r => U.rs(r.low) },
      { label: 'Close', num: true, render: r => U.rs(r.close) },
      { label: 'Average', num: true, render: r => U.rs(r.avg) },
    ], [...s.monthly].reverse().slice(0, 24)));
    U.html('histMeta', `<div class="card-note">${s.dates.length} stored observations ·
      source: ${U.esc(s.source)} · ${U.dataClassTag(s.data_class)}
      <br>The snapshot stores weekly points beyond six months and daily points inside it,
      to keep this file small without losing recent detail.</div>`);
  }

  function statBlock(s) {
    const v = s.volatility, b = s.band, ma = s.moving_averages, t = s.trend;
    return `<dl class="kv">
      <dt>As of</dt><dd>${U.esc(s.as_of)}</dd>
      <dt>1 day</dt><dd>${U.pct(s.changes['1D'])}</dd>
      <dt>7 days</dt><dd>${U.pct(s.changes['7D'])}</dd>
      <dt>30 days</dt><dd>${U.pct(s.changes['30D'])}</dd>
      <dt>90 days</dt><dd>${U.pct(s.changes['90D'])}</dd>
      <dt>1 year</dt><dd>${U.pct(s.changes['365D'])}</dd>
      <dt>7 DMA</dt><dd>${U.rs(ma.ma7)}</dd>
      <dt>30 DMA</dt><dd>${U.rs(ma.ma30)}</dd>
      <dt>90 DMA</dt><dd>${U.rs(ma.ma90)}</dd>
      <dt>200 DMA</dt><dd>${U.rs(ma.ma200)}</dd>
      <dt>Annualised vol</dt><dd>${U.num(v.annualised, 1)}% <span class="tag ${v.band === 'HIGH' || v.band === 'EXTREME' ? 'bad' : v.band === 'MEDIUM' ? 'warn' : 'good'}">${U.esc(v.band)}</span></dd>
      <dt>Percentile</dt><dd>${U.num(b.percentile, 0)}% <span class="tag ${b.percentile > 70 ? 'bad' : b.percentile < 30 ? 'good' : 'flat'}">${U.esc(b.zone)}</span></dd>
      <dt>RSI (14)</dt><dd>${U.num(s.momentum?.rsi14, 1)}</dd>
      <dt>Trend</dt><dd><b>${U.esc(t.trend)}</b> (${U.num(t.score, 0)})</dd>
    </dl>`;
  }

  /* ---------------------------------------------------------- forecast */
  function forecast() {
    const sel = U.el('fcSeries');
    if (!sel.options.length) {
      sel.innerHTML = D.series.filter(s => s.forecast?.length)
        .map(s => `<option value="${s.key}">${U.esc(s.label)}</option>`).join('');
      sel.onchange = () => { state.fcKey = sel.value; forecast(); };
    }
    state.fcKey = state.fcKey || (D.series.find(s => s.forecast?.length) || {}).key;
    if (!state.fcKey) return Charts.empty('chartForecast', 'No stored forecast in this snapshot');
    sel.value = state.fcKey;
    const s = seriesByKey[state.fcKey];

    const cut = new Date(s.as_of); cut.setDate(cut.getDate() - 260);
    const iso = cut.toISOString().slice(0, 10);
    const start = Math.max(s.dates.findIndex(d => d >= iso), 0);
    const history = s.dates.slice(start).map((d, i) => ({ date: d, price: s.prices[start + i] }));
    Charts.forecastChart('chartForecast', history, s.forecast_path || []);

    U.html('forecastTable', U.table([
      { label: 'Horizon', render: r => `${r.horizon_days} days` },
      { label: 'Target', render: r => U.date(r.target_date) },
      { label: 'Forecast', num: true, render: r => `<b>${U.rs(r.forecast_price)}</b>` },
      { label: 'vs today', num: true, render: r => U.chg((r.forecast_price - s.current) / s.current * 100) },
      { label: 'Lower 95%', num: true, render: r => U.rs(r.lower_ci) },
      { label: 'Upper 95%', num: true, render: r => U.rs(r.upper_ci) },
      { label: 'Direction', render: r => `<span class="tag ${r.direction === 'UP' ? 'bad' : r.direction === 'DOWN' ? 'good' : 'flat'}">${U.esc(r.direction)}</span>` },
      { label: 'Model', key: 'model_used', cls: 'mono' },
      { label: 'MAPE %', num: true, render: r => U.num(r.mape, 2) },
      { label: 'Confidence', render: r => `<span class="tag ${r.confidence === 'HIGH' ? 'good' : r.confidence === 'LOW' ? 'bad' : 'warn'}">${U.esc(r.confidence)}</span>` },
    ], s.forecast));

    const t = s.trend, v = s.volatility, b = s.band;
    U.html('fcContext', `<div class="grid g3">
      <div class="card"><div class="card-head"><h3>Trend classification</h3></div>
        <div><span class="signal ${t.score > 15 ? 'nolock' : t.score < -15 ? 'buy' : 'monitor'}">${U.esc(t.trend)}</span></div>
        <dl class="kv" style="margin-top:9px">${Object.entries(t.components || {}).map(([k, val]) =>
          `<dt>${U.esc(k.replace('_', ' '))} (${Math.round((t.weights[k] || 0) * 100)}%)</dt><dd>${val > 0 ? '+' : ''}${U.num(val, 0)}</dd>`).join('')}
          <dt><b>Composite</b></dt><dd><b>${U.num(t.score, 1)}</b></dd></dl></div>
      <div class="card"><div class="card-head"><h3>Volatility</h3></div>
        <div id="gaugeVol" class="chart sm" style="height:150px"></div>
        <dl class="kv"><dt>7-day</dt><dd>${U.num(v.v7, 1)}%</dd>
          <dt>30-day</dt><dd>${U.num(v.v30, 1)}%</dd>
          <dt>90-day</dt><dd>${U.num(v.v90, 1)}%</dd>
          <dt>Annualised</dt><dd>${U.num(v.annualised, 1)}%</dd>
          <dt>Band</dt><dd><span class="tag ${v.band === 'HIGH' || v.band === 'EXTREME' ? 'bad' : v.band === 'MEDIUM' ? 'warn' : 'good'}">${U.esc(v.band)}</span></dd></dl></div>
      <div class="card"><div class="card-head"><h3>Procurement price band</h3></div>
        <div class="k-value mono" style="font-size:19px">${U.esc(b.zone)}</div>
        <div class="faint" style="margin-bottom:8px">${U.num(b.percentile, 0)}th percentile of ${b.observations} observations</div>
        <div class="bar-track" style="height:8px"><div class="bar-fill ${b.percentile > 70 ? 'bad' : b.percentile < 30 ? 'good' : 'warn'}" style="width:${b.percentile}%"></div></div>
        <dl class="kv" style="margin-top:9px">
          <dt>Historical low</dt><dd>${U.rs(b.low)}</dd>
          <dt>Average</dt><dd>${U.rs(b.average)}</dd>
          <dt>Current</dt><dd><b>${U.rs(b.current)}</b></dd>
          <dt>Historical high</dt><dd>${U.rs(b.high)}</dd></dl></div></div>`);
    Charts.gauge('gaugeVol', v.index, v.band);
  }

  /* --------------------------------------------------------------- bom */
  function bom() {
    const sel = U.el('bomCommodity');
    if (!sel.onchange) sel.onchange = () => { state.bomC = sel.value; bom(); };
    sel.value = state.bomC;

    U.html('bomTransformers', U.table([
      { label: 'Job', key: 'job_no', cls: 'mono' },
      { label: 'Customer', render: r => U.esc(r.customer || '—') },
      { label: 'MVA', num: true, render: r => U.num(r.rating_mva, 1) },
      { label: 'Ratio', render: r => U.esc(r.voltage_ratio || '') },
      { label: 'Qty', num: true, key: 'quantity' },
      { label: 'Cu MT', num: true, render: r => U.num(r.copper_mt_project, 3) },
      { label: 'Al MT', num: true, render: r => U.num(r.aluminium_mt_project, 3) },
      { label: 'Material/unit', num: true, render: r => U.inr(r.material_cost_per_unit) },
      { label: 'vs BOM rate', num: true, render: r => U.chg(r.material_variance_pct) },
      { label: 'Project cost', num: true, render: r => U.inr(r.project_total_cost) },
      { label: 'Delivery', render: r => U.date(r.delivery_date) },
      { label: 'Status', render: r => `<span class="tag flat">${U.esc(r.status || '')}</span>` },
    ], D.rollup.transformers));

    const byC = D.rollup.by_commodity;
    U.html('bomByCommodity', U.table([
      { label: 'Commodity', key: 'commodity', cls: 'mono' },
      { label: 'Requirement MT', num: true, render: r => U.num(r.requirement_mt, 3) },
      { label: 'Current value', num: true, render: r => U.inr(r.current_value) },
      { label: 'At BOM rate', num: true, render: r => U.inr(r.bom_value) },
      { label: 'Variance', num: true, render: r => U.inr(r.variance) },
      { label: 'Variance %', num: true, render: r => U.chg(r.variance_pct) },
    ], Object.entries(byC).map(([k, v]) => ({ commodity: k, ...v }))));

    Charts.bars('chartExposureMix', Object.keys(byC),
      [{ name: 'Current value', data: Object.values(byC).map(v => v.current_value) }], { unit: 'INR' });

    const w = D.waterfall;
    if (w?.available) {
      Charts.waterfall('chartWaterfall', w.steps, w.original_cost_per_transformer);
      U.html('waterfallMeta', `<dl class="kv">
        <dt>Scope</dt><dd>${U.esc(w.label)}</dd>
        <dt>Base cost</dt><dd>${U.inr(w.base_cost)}</dd>
        <dt>Material cost</dt><dd>${U.inr(w.material_cost)}</dd>
        <dt>Original / transformer</dt><dd>${U.inr(w.original_cost_per_transformer)}</dd>
        <dt>Commodity impact</dt><dd>${U.inr(w.total_impact_per_transformer)}</dd>
        <dt>Revised / transformer</dt><dd><b>${U.inr(w.revised_cost_per_transformer)}</b></dd>
        <dt>Increase</dt><dd>${U.pct(w.increase_pct)}</dd>
        <dt>Project impact</dt><dd>${U.inr(w.project_impact)}</dd></dl>`);
    }

    const s = D.sensitivity[state.bomC];
    if (s?.available) {
      Charts.multiLine('chartSensitivity', s.rows.map(r => `${r.scenario_pct > 0 ? '+' : ''}${r.scenario_pct}%`),
        [{ name: 'Transformer cost', data: s.rows.map(r => r.transformer_cost), color: Charts.C.amber }],
        { unit: 'INR / transformer' });
      U.html('sensTable', U.table([
        { label: 'Scenario', render: r => `${r.scenario_pct > 0 ? '+' : ''}${r.scenario_pct}%` },
        { label: 'Price', num: true, render: r => U.rs(r.price) },
        { label: 'Transformer cost', num: true, render: r => U.inr(r.transformer_cost) },
        { label: 'Δ / transformer', num: true, render: r =>
            `<span class="chg ${U.dirClass(r.transformer_delta)}">${U.inr(r.transformer_delta)}</span>` },
        { label: 'Project exposure', num: true, render: r => U.inr(r.project_exposure) },
      ], s.rows, { rowClass: r => r.is_current ? 'best' : '' }));
    }

    Charts.scatter('chartMatrix', D.matrix.items);

    const f = D.flow[state.bomC];
    if (f) {
      const step = (l, v) => `<div class="flow-step"><span class="f-label">${l}</span><span class="f-value">${v}</span></div>`;
      U.html('flowChain', [
        step(`${f.commodity} price (${U.esc(f.provider)})`, U.rs(f.price) + ' /MT'),
        step('BOM consumption / transformer', U.num(f.consumption_mt_per_transformer, 4) + ' MT'),
        step('Material cost / transformer', U.inr(f.material_cost_per_transformer)),
        step('Transformer quantity', f.quantity + ' nos'),
        step('Total commodity exposure', U.inr(f.total_exposure)),
        step('Forecast price (30d)', U.rs(f.forecast_price) + ' /MT'),
        step('Forecast exposure', U.inr(f.forecast_exposure)),
        step('Potential exposure', U.inr(f.potential_exposure)),
        step('Recommendation', `<span class="signal ${U.signalClass(f.recommendation)}">${U.esc(f.recommendation)}</span>`),
      ].join('<div class="flow-arrow"></div>'));
    }

    const l = D.price_lock[state.bomC];
    if (l?.available) {
      U.html('lockResult', `<dl class="kv">
          <dt>Quantity</dt><dd>${U.num(l.quantity_mt, 3)} MT</dd>
          <dt>Lock price</dt><dd><b>${U.rs(l.lock_price)}</b></dd>
          <dt>Lock now cost</dt><dd>${U.inr(l.lock_now_cost)}</dd>
          <dt>Expected price</dt><dd>${U.rs(l.expected_price)}</dd>
          <dt>Annualised vol</dt><dd>${U.num(l.annualised_volatility, 1)}%</dd>
          <dt>Horizon sigma</dt><dd>${U.num(l.horizon_sigma_pct, 2)}%</dd></dl>
        ${U.table([
          { label: 'Scenario', key: 'scenario' },
          { label: 'Price', num: true, render: x => U.rs(x.price) },
          { label: 'Wait cost', num: true, render: x => U.inr(x.wait_cost) },
          { label: 'vs lock', num: true, render: x => `<span class="chg ${x.vs_lock >= 0 ? 'down' : 'up'}">${U.inr(x.vs_lock)}</span>` },
        ], l.scenarios)}
        <div class="card-note"><b>Recommended lock ${U.num(l.recommended_lock_pct, 0)}%
          (${U.num(l.recommended_lock_mt, 3)} MT).</b> Best/worst are the 5th and 95th
          percentiles implied by realised volatility over the lock period.</div>`);
    } else {
      U.html('lockResult', '<div class="chart-empty">No lock analysis for this commodity</div>');
    }
  }

  /* what-if runs entirely in the browser off the snapshot's BOM figures */
  function whatIf(ev) {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target).entries());
    const c = f.commodity;
    const agg = D.rollup.by_commodity[c];
    if (!agg) { U.html('whatIfResult', '<div class="banner err">No BOM consumption for this commodity.</div>'); return; }
    const units = f.quantity ? +f.quantity : (D.rollup.totals.units || 1);
    const cons = f.consumption_mt ? +f.consumption_mt
      : agg.requirement_mt / (D.rollup.totals.units || 1);
    const row = D.impact.rows.find(r => r.commodity === c);
    const cur = row ? row.current_price : 0;
    const scenario = +f.scenario_price;
    const delta = scenario - cur;
    const perUnit = delta * cons, project = perUnit * units;
    const refCost = (D.rollup.totals.base_cost + D.rollup.totals.current_material_cost) /
      (D.rollup.totals.units || 1);
    U.html('whatIfResult', `<dl class="kv">
      <dt>Current price</dt><dd>${U.rs(cur)} /MT</dd>
      <dt>Scenario price</dt><dd>${U.rs(scenario)} /MT</dd>
      <dt>Price change</dt><dd><span class="chg ${U.dirClass(delta)}">${U.rs(delta)} (${U.pct(cur ? delta / cur * 100 : 0)})</span></dd>
      <dt>BOM consumption</dt><dd>${U.num(cons, 4)} MT / transformer</dd>
      <dt>Transformer quantity</dt><dd>${units}</dd>
      <dt><b>Impact / transformer</b></dt><dd><b>${U.inr(perUnit)}</b></dd>
      <dt><b>Total project impact</b></dt><dd><b>${U.inr(project)}</b></dd>
      <dt>Reference transformer cost</dt><dd>${U.inr(refCost)}</dd>
      <dt>Transformer cost increase</dt><dd>${U.pct(refCost ? perUnit / refCost * 100 : 0)}</dd>
    </dl><div class="card-note">Impact = (scenario price − current price) × BOM consumption × quantity.
      Computed in your browser from the snapshot's BOM figures.</div>`);
  }

  /* ------------------------------------------------------------ quotes */
  function quotes() {
    U.html('quotesTable', U.table([
      { label: 'Supplier', render: r => U.esc(r.supplier) },
      { label: 'Commodity', render: r => `${U.esc(r.commodity)} / ${U.esc(r.product || '—')}` },
      { label: 'Quote date', render: r => U.date(r.quote_date) },
      { label: 'Valid until', render: r => r.valid_until ?
          `${U.date(r.valid_until)} ${r.expired ? '<span class="tag bad">expired</span>' : ''}` : '—' },
      { label: 'Quoted', num: true, render: r => `${U.num(r.price, 0)} ${U.esc(r.currency)}/${U.esc(r.unit)}` },
      { label: 'Basic INR/MT', num: true, render: r => U.rs(r.effective_price) },
      { label: 'Landed cost', num: true, render: r => `<b>${U.rs(r.landed_cost)}</b>` },
      { label: 'Benchmark', num: true, render: r => U.rs(r.benchmark) },
      { label: 'vs market', num: true, render: r => U.chg(r.variance_pct) },
      { label: 'MOQ', num: true, render: r => r.moq_mt ? U.num(r.moq_mt, 1) + ' MT' : '—' },
      { label: 'Terms', render: r => U.esc(r.payment_terms || '—') },
      { label: 'Status', render: r => {
          const m = { 'BEST PRICE': 'good', 'BELOW MARKET': 'good',
                      'ABOVE MARKET': 'bad', 'EXPIRED QUOTE': 'warn' };
          return r.status_flag ? `<span class="tag ${m[r.status_flag] || 'flat'}">${U.esc(r.status_flag)}</span>` : '—'; } },
    ], D.quotes, { rowClass: r => r.status_flag === 'BEST PRICE' ? 'best' : '' }));
  }

  /* ------------------------------------------------------------ alerts */
  function renderAlerts(target, list) {
    if (!list?.length) { U.html(target, '<div class="chart-empty">No open alerts</div>'); return; }
    U.html(target, list.map(a => `
      <div class="alert-row ${U.esc(a.severity)}">
        <div class="a-body">
          <div class="a-title">${U.esc(a.title)}
            <span class="tag ${a.severity === 'CRITICAL' ? 'bad' : a.severity === 'WARNING' ? 'warn' : 'info'}">${U.esc(a.severity)}</span></div>
          <div class="a-msg">${U.esc(a.message)}</div></div>
        <span class="a-time">${U.ago(a.triggered_at)}</span></div>`).join(''));
  }
  function alerts() { renderAlerts('alertList', D.alerts); }

  /* --------------------------------------------------------------- kpi */
  function kpi() {
    U.html('procKpis', Object.values(D.proc_kpis.kpis).map(k => `
      <div class="card"><div class="card-head"><h3>${k.commodity} — supply chain KPIs</h3></div>
        <dl class="kv">
          <dt>Market price</dt><dd>${U.rs(k.market_price)}</dd>
          <dt>Benchmark average</dt><dd>${U.rs(k.benchmark_avg_period)}</dd>
          <dt>Average buying price</dt><dd>${U.rs(k.average_buying_price)}</dd>
          <dt>Purchase timing gain / MT</dt><dd>${U.rs(k.purchase_timing_gain_per_mt)}</dd>
          <dt>Price avoidance</dt><dd>${U.inr(k.price_avoidance)}</dd>
          <dt>Quantity purchased</dt><dd>${U.num(k.quantity_purchased_mt, 2)} MT</dd>
          <dt>Total spend</dt><dd>${U.inr(k.total_spend)}</dd>
          <dt>BOM rate</dt><dd>${U.rs(k.bom_rate)}</dd>
          <dt>BOM vs market</dt><dd>${U.pct(k.bom_vs_market_pct)}</dd>
          <dt>Market vs PO rate</dt><dd>${U.pct(k.market_vs_po_pct)}</dd>
          <dt>Coverage</dt><dd>${U.num(k.coverage_pct, 1)}%</dd>
          <dt>Open PO coverage</dt><dd>${U.num(k.open_po_coverage_pct, 1)}%</dd>
          <dt>Uncovered exposure</dt><dd>${U.inr(k.uncovered_exposure)}</dd>
          <dt>Best supplier</dt><dd>${U.esc(k.best_supplier || '—')}</dd>
          <dt>Best landed</dt><dd>${U.rs(k.best_supplier_landed)}</dd>
          <dt>Worst supplier</dt><dd>${U.esc(k.worst_supplier || '—')}</dd>
          <dt>Supplier variance</dt><dd>${U.pct(k.supplier_price_variance_pct)}</dd>
          <dt>Forecast MAPE</dt><dd>${U.num(k.forecast_accuracy_mape, 2)}%</dd>
        </dl></div>`).join(''));

    const b = D.backtest;
    if (b?.available) {
      Charts.multiLine('chartCostBacktest', b.series.map(p => p.date), [
        { name: 'Total transformer cost', data: b.series.map(p => p.total_cost), color: Charts.C.amber },
        { name: 'Material cost only', data: b.series.map(p => p.material_cost), color: Charts.C.blue },
      ], { unit: 'INR / transformer', zoom: true });
      U.html('backtestMeta', `<dl class="kv">
        <dt>Scope</dt><dd>${U.esc(b.label)}</dd>
        <dt>Lowest cost</dt><dd>${U.inr(b.min_cost)}</dd>
        <dt>Highest cost</dt><dd>${U.inr(b.max_cost)}</dd>
        <dt>Current cost</dt><dd><b>${U.inr(b.current_cost)}</b></dd>
        <dt>Peak-to-trough swing</dt><dd>${U.num(b.swing_pct, 1)}%</dd>
      </dl><div class="card-note">${U.esc(b.note)}</div>`);
    }
  }

  /* -------------------------------------------------------- csv export */
  function exportCsv() {
    const s = seriesByKey[state.histKey || D.series[0].key];
    const rows = [['date', 'price_inr_mt', 'commodity', 'provider', 'product',
                   'source', 'data_class'].join(',')];
    s.dates.forEach((d, i) => rows.push([d, s.prices[i], s.commodity, s.provider,
      s.product, `"${(s.source || '').replace(/"/g, '""')}"`, s.data_class].join(',')));
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${s.commodity}_${s.provider}_${s.product}_${s.as_of}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }

  const renderers = { dashboard, copper, aluminium, historical, forecast, bom,
                      quotes, alerts, kpi };

  /* ------------------------------------------------------- freshness ----
   * When this page is served from a web address it polls a tiny version file
   * and reloads itself if a newer build has been published. The file is a few
   * hundred bytes, so a 5-minute poll costs almost nothing - reloading the
   * 1.6 MB page itself on a timer would burn gigabytes an hour and show the
   * same numbers every time, because the build only changes once a day.
   *
   * Opened from disk (file://) there is nothing to poll, so this quietly does
   * nothing and the page stays exactly as self-contained as before.
   * -------------------------------------------------------------------- */
  const FRESHNESS_POLL_MS = 5 * 60 * 1000;

  function ageText(iso) {
    const mins = (Date.now() - new Date(iso).getTime()) / 60000;
    if (mins < 90) return `${Math.max(0, Math.round(mins))} min old`;
    const hrs = mins / 60;
    if (hrs < 36) return `${Math.round(hrs)} h old`;
    return `${Math.round(hrs / 24)} days old`;
  }

  function paintFreshness() {
    const el = U.el('freshness');
    if (!el) return;
    const built = new Date(D.generated_at);
    const stale = (Date.now() - built.getTime()) > 36 * 3600 * 1000;
    el.className = 'pill ' + (stale ? 'stale' : 'live');
    el.title = `Built ${built.toLocaleString('en-IN')}`;
    el.innerHTML = `<span class="dot"></span>${ageText(D.generated_at)}`;
  }

  async function checkForNewBuild() {
    if (location.protocol === 'file:') return;          // opened from disk
    try {
      const res = await fetch('version.json?t=' + Date.now(), { cache: 'no-store' });
      if (!res.ok) return;
      const v = await res.json();
      if (v.generated_at && v.generated_at !== D.generated_at) {
        const n = U.el('freshness');
        if (n) {
          n.className = 'pill demo';
          n.innerHTML = '<span class="dot"></span>New build - refreshing';
        }
        setTimeout(() => location.reload(), 1200);
      }
    } catch (e) {
      /* offline, or no version file: leave the page alone */
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav button').forEach(b => b.onclick = () => go(b.dataset.page));
    banner();
    paintFreshness();
    setInterval(paintFreshness, 60000);
    if (location.protocol !== 'file:') {
      checkForNewBuild();
      setInterval(checkForNewBuild, FRESHNESS_POLL_MS);
    }
    const p = U.el('statusPill');
    if (p) p.innerHTML = `<span class="dot"></span>${new Date(D.generated_at).toLocaleDateString('en-IN')}`;
    dashboard();
  });

  return { go, whatIf, exportCsv, data: D };
})();
