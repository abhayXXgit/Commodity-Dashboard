/* ECharts wrappers. One theme object, one resize observer, graceful
   degradation when the CDN is unreachable (offline / air-gapped desks). */
const Charts = (() => {
  const registry = new Map();
  const C = {
    text: '#E8EFF9', dim: '#9AAECA', faint: '#63788F',
    grid: '#1B2942', border: '#243449', surface: '#111C2E',
    amber: '#F0A202', blue: '#4C8DF6', teal: '#19B8A6', violet: '#9B7CF0',
    good: '#2FBF71', bad: '#F2545B', flat: '#7C8EA6',
  };
  const PALETTE = [C.amber, C.blue, C.teal, C.violet, C.good, '#E8778E', '#68B7DB'];

  function available() { return typeof echarts !== 'undefined'; }

  function base(extra = {}) {
    return Object.assign({
      backgroundColor: 'transparent',
      color: PALETTE,
      textStyle: { fontFamily: 'IBM Plex Sans, sans-serif', color: C.dim, fontSize: 11 },
      animationDuration: 400,
      grid: { left: 62, right: 20, top: 30, bottom: 34, containLabel: false },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(16,26,43,.96)',
        borderColor: C.border, borderWidth: 1,
        textStyle: { color: C.text, fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' },
        axisPointer: { type: 'cross', label: { backgroundColor: '#22314a' },
                       lineStyle: { color: C.faint, type: 'dashed' } },
      },
      legend: { textStyle: { color: C.dim, fontSize: 10 }, top: 0, right: 0,
                itemWidth: 14, itemHeight: 8, icon: 'roundRect' },
    }, extra);
  }
  const axisX = (o = {}) => Object.assign({
    type: 'category', boundaryGap: false,
    axisLine: { lineStyle: { color: C.border } },
    axisLabel: { color: C.faint, fontSize: 10, hideOverlap: true },
    axisTick: { show: false }, splitLine: { show: false },
  }, o);
  const axisY = (o = {}) => Object.assign({
    type: 'value', scale: true,
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: C.faint, fontSize: 10,
      formatter: v => Math.abs(v) >= 1e7 ? (v / 1e7).toFixed(1) + 'Cr'
        : Math.abs(v) >= 1e5 ? (v / 1e5).toFixed(1) + 'L'
        : Math.abs(v) >= 1000 ? (v / 1000).toFixed(0) + 'k' : v },
    splitLine: { lineStyle: { color: C.grid, type: 'dashed' } },
  }, o);

  function mount(id, option) {
    const node = document.getElementById(id);
    if (!node) return null;
    if (!available()) {
      node.innerHTML = '<div class="chart-empty">Charting library unavailable.<br>' +
        'The dashboard still works — every figure is in the tables below.</div>';
      return null;
    }
    let inst = registry.get(id);
    if (!inst || inst.isDisposed?.()) {
      inst = echarts.init(node, null, { renderer: 'canvas' });
      registry.set(id, inst);
    }
    inst.setOption(option, true);
    return inst;
  }
  function empty(id, msg) {
    const node = document.getElementById(id);
    if (node) node.innerHTML = `<div class="chart-empty">${msg}</div>`;
    const inst = registry.get(id);
    if (inst) { inst.dispose(); registry.delete(id); }
  }
  function resizeAll() { registry.forEach(i => { try { i.resize(); } catch (e) {} }); }
  window.addEventListener('resize', () => { clearTimeout(window.__crt);
    window.__crt = setTimeout(resizeAll, 120); });

  /* ---------------- price history + moving averages ---------------- */
  function priceLine(id, points, opts = {}) {
    if (!points || !points.length) return empty(id, opts.empty || 'No price history');
    const dates = points.map(p => p.date);
    const series = [{
      name: opts.name || 'Price', type: 'line', data: points.map(p => p.price),
      showSymbol: false, lineStyle: { width: 2, color: C.amber },
      itemStyle: { color: C.amber },
      areaStyle: opts.area === false ? null : {
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
          { offset: 0, color: 'rgba(240,162,2,.22)' },
          { offset: 1, color: 'rgba(240,162,2,0)' }] } },
    }];
    const maCfg = [['ma7', '7 DMA', C.blue], ['ma30', '30 DMA', C.teal],
                   ['ma90', '90 DMA', C.violet], ['ma200', '200 DMA', C.flat]];
    (opts.mas || ['ma30', 'ma90']).forEach(key => {
      const cfg = maCfg.find(m => m[0] === key);
      if (!cfg || !points.some(p => p[key] != null)) return;
      series.push({ name: cfg[1], type: 'line', data: points.map(p => p[key] ?? null),
        showSymbol: false, connectNulls: true,
        lineStyle: { width: 1.2, color: cfg[2], type: 'dashed' },
        itemStyle: { color: cfg[2] } });
    });
    return mount(id, base({
      xAxis: axisX({ data: dates }), yAxis: axisY({ name: opts.unit || 'INR / MT',
        nameTextStyle: { color: C.faint, fontSize: 10 }, nameGap: 12 }),
      series,
      dataZoom: opts.zoom === false ? undefined
        : [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 6,
             borderColor: C.border, backgroundColor: C.surface,
             fillerColor: 'rgba(76,141,246,.14)',
             handleStyle: { color: C.blue }, textStyle: { color: C.faint, fontSize: 9 } }],
      grid: { left: 62, right: 20, top: 30, bottom: opts.zoom === false ? 34 : 54 },
    }));
  }

  /* ---------------- forecast with confidence band ---------------- */
  function forecastChart(id, history, path, opts = {}) {
    if (!history || !history.length) return empty(id, 'No history to project from');
    const hDates = history.map(p => p.date);
    const fDates = (path || []).map(p => p.date);
    const dates = hDates.concat(fDates);
    const nH = hDates.length;
    const pad = new Array(nH - 1).fill(null);
    const link = history[nH - 1]?.price ?? null;

    const fc = pad.concat([link], (path || []).map(p => p.forecast));
    const lower = pad.concat([link], (path || []).map(p => p.lower));
    const bandW = pad.concat([0], (path || []).map(p => p.upper - p.lower));

    return mount(id, base({
      legend: { data: ['Actual', 'Forecast', '95% band'], textStyle: { color: C.dim, fontSize: 10 },
                top: 0, right: 0, itemWidth: 14, itemHeight: 8, icon: 'roundRect' },
      xAxis: axisX({ data: dates }),
      yAxis: axisY({ name: 'INR / MT', nameTextStyle: { color: C.faint, fontSize: 10 } }),
      series: [
        { name: '95% band', type: 'line', data: lower, stack: 'band', showSymbol: false,
          lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true,
          tooltip: { show: false } },
        { name: '95% band', type: 'line', data: bandW, stack: 'band', showSymbol: false,
          lineStyle: { opacity: 0 }, silent: true,
          areaStyle: { color: 'rgba(76,141,246,.16)' } },
        { name: 'Actual', type: 'line', data: history.map(p => p.price).concat(
            new Array(fDates.length).fill(null)),
          showSymbol: false, lineStyle: { width: 2, color: C.amber },
          itemStyle: { color: C.amber } },
        { name: 'Forecast', type: 'line', data: fc, showSymbol: false, connectNulls: true,
          lineStyle: { width: 2, color: C.blue, type: 'dashed' }, itemStyle: { color: C.blue } },
      ],
    }));
  }

  /* ---------------- multi-series comparison ---------------- */
  function multiLine(id, dates, series, opts = {}) {
    if (!dates || !dates.length) return empty(id, opts.empty || 'No data');
    return mount(id, base({
      xAxis: axisX({ data: dates }),
      yAxis: axisY({ name: opts.unit || 'INR / MT', nameTextStyle: { color: C.faint, fontSize: 10 } }),
      series: series.map((s, i) => ({
        name: s.name, type: 'line', data: s.data, showSymbol: false,
        lineStyle: { width: 1.8, color: s.color || PALETTE[i % PALETTE.length] },
        itemStyle: { color: s.color || PALETTE[i % PALETTE.length] },
      })),
      dataZoom: opts.zoom ? [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 4,
        borderColor: C.border, fillerColor: 'rgba(76,141,246,.14)' }] : undefined,
      grid: { left: 62, right: 20, top: 30, bottom: opts.zoom ? 48 : 34 },
    }));
  }

  /* ---------------- bars / stacked bars ---------------- */
  function bars(id, categories, series, opts = {}) {
    if (!categories || !categories.length) return empty(id, opts.empty || 'No data');
    return mount(id, base({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(16,26,43,.96)', borderColor: C.border,
        textStyle: { color: C.text, fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' } },
      xAxis: axisX({ type: 'category', boundaryGap: true, data: categories,
        axisLabel: { color: C.faint, fontSize: 10, interval: 0, rotate: opts.rotate || 0 } }),
      yAxis: axisY({ name: opts.unit || '', nameTextStyle: { color: C.faint, fontSize: 10 } }),
      series: series.map((s, i) => ({
        name: s.name, type: 'bar', data: s.data,
        stack: opts.stack ? 'total' : undefined,
        barMaxWidth: 34,
        itemStyle: { color: s.color || PALETTE[i % PALETTE.length],
                     borderRadius: opts.stack ? 0 : [3, 3, 0, 0] },
      })),
    }));
  }

  /* ---------------- waterfall (§25.8) ---------------- */
  function waterfall(id, steps, base0, opts = {}) {
    if (!steps || !steps.length) return empty(id, 'No BOM steps');
    const cats = ['Original cost'], invisible = [0], pos = [base0], neg = ['-'];
    let run = base0;
    steps.forEach(s => {
      cats.push(s.commodity);
      if (s.impact >= 0) { invisible.push(run); pos.push(s.impact); neg.push('-'); }
      else { invisible.push(run + s.impact); pos.push('-'); neg.push(-s.impact); }
      run += s.impact;
    });
    cats.push('Revised cost'); invisible.push(0); pos.push(run); neg.push('-');
    return mount(id, base({
      legend: { show: false },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(16,26,43,.96)', borderColor: C.border,
        textStyle: { color: C.text, fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' },
        formatter: (p) => {
          const bar = p.find(x => x.seriesName !== 'placeholder' && x.value !== '-');
          if (!bar) return '';
          return `${bar.name}<br/>₹${Number(bar.value).toLocaleString('en-IN',
            { maximumFractionDigits: 0 })}`;
        } },
      xAxis: axisX({ type: 'category', boundaryGap: true, data: cats,
        axisLabel: { color: C.faint, fontSize: 10, interval: 0, rotate: 24 } }),
      yAxis: axisY({ name: 'INR / transformer', nameTextStyle: { color: C.faint, fontSize: 10 } }),
      series: [
        { name: 'placeholder', type: 'bar', stack: 'w', silent: true,
          itemStyle: { color: 'transparent' }, data: invisible, tooltip: { show: false } },
        { name: 'increase', type: 'bar', stack: 'w', data: pos, barMaxWidth: 40,
          itemStyle: { color: (p) => (p.dataIndex === 0 || p.dataIndex === cats.length - 1)
            ? C.blue : C.bad } },
        { name: 'decrease', type: 'bar', stack: 'w', data: neg, barMaxWidth: 40,
          itemStyle: { color: C.good } },
      ],
      grid: { left: 66, right: 20, top: 18, bottom: 62 },
    }));
  }

  /* ---------------- risk / exposure scatter (§25.20) ---------------- */
  function scatter(id, items, opts = {}) {
    if (!items || !items.length) return empty(id, 'No projects to plot');
    const groups = {};
    items.forEach(i => { (groups[i.commodity] ||= []).push(i); });
    const series = Object.entries(groups).map(([c, arr], idx) => ({
      name: c, type: 'scatter',
      symbolSize: (d) => Math.max(9, Math.min(38, Math.sqrt(d[1] / 1e5) * 4)),
      data: arr.map(i => [i.risk_score, i.exposure, i.job_no, i.critical, i.rating_mva]),
      itemStyle: { color: PALETTE[idx % PALETTE.length], opacity: .85,
        borderColor: (p) => p.data[3] ? C.bad : 'transparent', borderWidth: 2 },
    }));
    return mount(id, base({
      tooltip: {
        trigger: 'item', backgroundColor: 'rgba(16,26,43,.96)', borderColor: C.border,
        textStyle: { color: C.text, fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' },
        formatter: (p) => `<b>${p.data[2]}</b> — ${p.seriesName}<br/>` +
          `${p.data[4]} MVA<br/>Risk ${p.data[0].toFixed(1)} / 100<br/>` +
          `Exposure ₹${(p.data[1] / 1e7).toFixed(2)} Cr` +
          (p.data[3] ? '<br/><b style="color:#F2545B">CRITICAL PRIORITY</b>' : ''),
      },
      xAxis: axisY({ name: 'Commodity price risk →', min: 0, max: 100,
        nameLocation: 'middle', nameGap: 24, nameTextStyle: { color: C.faint, fontSize: 10 },
        splitLine: { lineStyle: { color: C.grid, type: 'dashed' } } }),
      yAxis: axisY({ name: 'Project exposure (INR)',
        nameTextStyle: { color: C.faint, fontSize: 10 } }),
      series,
      grid: { left: 66, right: 24, top: 26, bottom: 46 },
    }));
  }

  /* ---------------- gauge for volatility index ---------------- */
  function gauge(id, value, label) {
    if (value == null) return empty(id, 'Volatility unavailable');
    return mount(id, {
      backgroundColor: 'transparent',
      series: [{
        type: 'gauge', startAngle: 200, endAngle: -20, min: 0, max: 100,
        radius: '96%', center: ['50%', '62%'], pointer: { width: 4, itemStyle: { color: C.text } },
        progress: { show: true, width: 10 },
        axisLine: { lineStyle: { width: 10, color: [
          [.3, C.good], [.55, C.amber], [.8, '#E8778E'], [1, C.bad]] } },
        axisTick: { show: false }, splitLine: { length: 8, lineStyle: { color: C.border } },
        axisLabel: { color: C.faint, fontSize: 9, distance: -18 },
        detail: { valueAnimation: true, fontSize: 20, fontFamily: 'IBM Plex Mono, monospace',
          color: C.text, offsetCenter: [0, '30%'], formatter: v => v.toFixed(0) },
        title: { offsetCenter: [0, '62%'], color: C.faint, fontSize: 10 },
        data: [{ value, name: label || 'Volatility index' }],
      }],
    });
  }

  return { mount, empty, priceLine, forecastChart, multiLine, bars, waterfall,
           scatter, gauge, resizeAll, available, C, PALETTE };
})();
