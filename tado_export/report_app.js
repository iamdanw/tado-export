// ---------------------------------------------------------------------------
// All filtering happens here, against data embedded in the page. Series are
// stored on a dense grid with nulls for missing samples, so gaps survive
// slicing and plotly breaks the lines on its own.
// ---------------------------------------------------------------------------
var CONFIG = { responsive: true, displaylogo: false,
               modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
var CHARTS = ['temp', 'humidity', 'demand', 'hours'];
var MIN_COVERAGE = 0.5;

var state = {
  zones: R.zones.map(function (z) { return String(z.id); }),
  i0: 0,
  i1: R.days.length - 1,
  preset: 0
};

function mode() {
  var stamped = document.documentElement.getAttribute('data-theme');
  if (stamped) return stamped;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function clone(o) { return JSON.parse(JSON.stringify(o)); }
function el(id) { return document.getElementById(id); }

function fmt(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return v.toLocaleString(undefined, { minimumFractionDigits: digits === undefined ? 1 : digits,
                                       maximumFractionDigits: digits === undefined ? 1 : digits });
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

function zoneById(id) {
  for (var i = 0; i < R.zones.length; i++) if (String(R.zones[i].id) === String(id)) return R.zones[i];
  return null;
}
function selectedZones() {
  return R.zones.filter(function (z) { return state.zones.indexOf(String(z.id)) !== -1; });
}

// -- series construction ----------------------------------------------------

function dailyMean(sum, n, i0, i1) {
  var x = [], y = [];
  for (var i = i0; i <= i1; i++) {
    x.push(R.days[i]);
    y.push(n[i] ? +(sum[i] / n[i]).toFixed(2) : null);
  }
  return { x: x, y: y };
}

function hourlySlice(values, h0, h1) {
  return { x: R.stamps.slice(h0, h1), y: values.slice(h0, h1) };
}

function nextDay(d) {
  var t = new Date(d + 'T00:00:00Z');
  t.setUTCDate(t.getUTCDate() + 1);
  return t.toISOString().slice(0, 10);
}

// Hourly buckets carry the label of the hour they start: the 23:00 point is the
// mean of 23:00-23:59. Slicing on the day boundary therefore ends the line at
// 23:00 and leaves the last hour looking absent. Pin the axis to whole days and
// draw one point past the end so the line actually reaches midnight.
function dayWindow() {
  return [R.days[state.i0] + 'T00:00', nextDay(R.days[state.i1]) + 'T00:00'];
}

function setRangeOn(layout, range) {
  ['xaxis', 'xaxis2'].forEach(function (k) {
    if (layout[k]) layout[k].range = range.slice();
  });
}

function tempTraces(useHourly, h0, h1, m) {
  var traces = [], zones = selectedZones();
  zones.forEach(function (z) {
    var key = String(z.id), s = R.series[key], sum = R.summary[key];
    var color = z.color[m];
    if (useHourly) {
      var sp = hourlySlice(s.setpoint, h0, h1);
      if (sp.y.some(function (v) { return v !== null; })) {
        traces.push({
          type: 'scatter', mode: 'lines', x: sp.x, y: sp.y, connectgaps: false,
          name: z.name + ' · target', legendgroup: z.name, opacity: 0.45,
          line: { color: color, width: 1.2, dash: 'dash', shape: 'hv' },
          xaxis: 'x', yaxis: 'y',
          hovertemplate: z.name + ' target: %{y:.1f} °C<extra></extra>'
        });
      }
    }
    var series = useHourly ? hourlySlice(s.temp, h0, h1)
                           : dailyMean(sum.tSum, sum.tN, state.i0, state.i1);
    traces.push({
      type: 'scatter', mode: 'lines', x: series.x, y: series.y, connectgaps: false,
      name: z.name, legendgroup: z.name,
      line: { color: color, width: 2 }, xaxis: 'x', yaxis: 'y',
      hovertemplate: z.name + ': %{y:.1f} °C<extra></extra>'
    });
  });

  var out = useHourly ? hourlySlice(R.outside.hourly, h0, h1)
                      : dailyMean(R.outside.sum, R.outside.n, state.i0, state.i1);
  if (out.y.some(function (v) { return v !== null; })) {
    traces.push({
      type: 'scatter', mode: 'lines', x: out.x, y: out.y, connectgaps: false,
      name: 'Outside', line: { color: R.reference[m], width: 1.5 },
      xaxis: 'x2', yaxis: 'y2',
      hovertemplate: 'Outside: %{y:.1f} °C<extra></extra>'
    });
  }
  return traces;
}

function humidityTraces(useHourly, h0, h1, m) {
  var traces = [];
  selectedZones().forEach(function (z) {
    var key = String(z.id), s = R.series[key], sum = R.summary[key];
    var series = useHourly ? hourlySlice(s.humidity, h0, h1)
                           : dailyMean(sum.hSum, sum.hN, state.i0, state.i1);
    if (!series.y.some(function (v) { return v !== null; })) return;   // e.g. hot water
    traces.push({
      type: 'scatter', mode: 'lines', x: series.x, y: series.y, connectgaps: false,
      name: z.name, line: { color: z.color[m], width: 2 },
      hovertemplate: z.name + ': %{y:.0f} %<extra></extra>'
    });
  });
  return traces;
}

// The heatmap gets its own zone control: averaging several zones dilutes a
// single hot one, and narrowing it here must not strip zones from the other
// charts. Options track the global selection; the value survives if still valid.
function syncDemandZones() {
  var box = el('demand-zone'), chosen = selectedZones(), previous = box.value;
  var options = ['<option value="all">Average of ' + chosen.length + ' selected zone' +
                 (chosen.length === 1 ? '' : 's') + '</option>'];
  chosen.forEach(function (z) {
    options.push('<option value="' + z.id + '">' + esc(z.name) + '</option>');
  });
  box.innerHTML = options.join('');
  var valid = previous === 'all' ||
              chosen.some(function (z) { return String(z.id) === previous; });
  box.value = valid && previous ? previous : 'all';
  return box.value;
}

function demandZones(choice) {
  if (!choice || choice === 'all') return selectedZones();
  var one = zoneById(choice);
  return one && state.zones.indexOf(String(one.id)) !== -1 ? [one] : selectedZones();
}

function demandTrace(m, zones) {
  var nDays = state.i1 - state.i0 + 1;
  var x = R.days.slice(state.i0, state.i1 + 1);
  var z = [], any = false;
  for (var h = 0; h < 24; h++) {
    var row = [];
    for (var d = 0; d < nDays; d++) {
      var sum = 0, n = 0;
      for (var k = 0; k < zones.length; k++) {
        var v = R.demand[String(zones[k].id)][(state.i0 + d) * 24 + h];
        if (v !== null && v !== undefined) { sum += v; n++; }
      }
      if (n) { row.push(+(sum / n).toFixed(3)); any = true; } else { row.push(null); }
    }
    z.push(row);
  }
  if (!any) return null;
  var scale = R.sequential.map(function (c, i) { return [i / (R.sequential.length - 1), c]; });
  return {
    type: 'heatmap', x: x, y: Array.from({ length: 24 }, function (_, i) { return i; }), z: z,
    colorscale: scale, zmin: 0, zmax: 3,
    xgap: nDays <= 120 ? 1 : 0, ygap: 1,
    hovertemplate: '%{x} at %{y}:00<br>demand %{z:.2f} / 3<extra></extra>',
    colorbar: { title: { text: 'Demand', font: { size: 11 } },
                tickvals: [0, 1, 2, 3], ticktext: ['none', 'low', 'med', 'high'],
                tickfont: { size: 10 }, outlinewidth: 0, thickness: 12, len: 0.8 }
  };
}

function hoursTraces(m) {
  var nDays = state.i1 - state.i0 + 1;
  var edge = nDays <= 60 ? 2 : 0;
  var traces = [], hidden = {};
  selectedZones().forEach(function (z) {
    var sum = R.summary[String(z.id)], x = [], y = [];
    for (var i = state.i0; i <= state.i1; i++) {
      var cov = sum.cov[i];
      if (cov === null || cov === undefined) continue;
      if (cov < MIN_COVERAGE) { hidden[R.days[i]] = true; continue; }
      x.push(R.days[i]); y.push(sum.hours[i]);
    }
    if (!x.length) return;
    traces.push({
      type: 'bar', x: x, y: y, name: z.name,
      marker: { color: z.color[m], line: { color: R.panel[m], width: edge } },
      hovertemplate: z.name + ': %{y:.1f} h<extra></extra>'
    });
  });
  return { traces: traces, hidden: Object.keys(hidden).length, nDays: nDays };
}

// -- tiles and tables -------------------------------------------------------

function aggregate() {
  var zones = selectedZones();
  var tSum = 0, tN = 0, tMin = null, tMax = null, hours = 0, readings = 0;
  var covered = {};
  zones.forEach(function (z) {
    var s = R.summary[String(z.id)];
    var isRoom = z.type !== 'HOT_WATER';
    for (var i = state.i0; i <= state.i1; i++) {
      if (s.tN[i]) {
        readings += s.tN[i];
        covered[i] = true;
        if (isRoom) {
          tSum += s.tSum[i]; tN += s.tN[i];
          if (s.tMin[i] !== null && (tMin === null || s.tMin[i] < tMin)) tMin = s.tMin[i];
          if (s.tMax[i] !== null && (tMax === null || s.tMax[i] > tMax)) tMax = s.tMax[i];
        }
      }
      if (s.hours[i] !== null && s.hours[i] !== undefined) hours += s.hours[i];
    }
  });
  var oSum = 0, oN = 0, oMin = null, oMax = null;
  for (var i = state.i0; i <= state.i1; i++) {
    if (R.outside.n[i]) {
      oSum += R.outside.sum[i]; oN += R.outside.n[i];
      if (R.outside.min[i] !== null && (oMin === null || R.outside.min[i] < oMin)) oMin = R.outside.min[i];
      if (R.outside.max[i] !== null && (oMax === null || R.outside.max[i] > oMax)) oMax = R.outside.max[i];
    }
  }
  return {
    inside: tN ? tSum / tN : null, tMin: tMin, tMax: tMax,
    outside: oN ? oSum / oN : null, oMin: oMin, oMax: oMax,
    hours: hours, readings: readings,
    days: Object.keys(covered).length, span: state.i1 - state.i0 + 1,
    zones: zones.length
  };
}

function renderTiles(a) {
  var tiles = [
    ['Avg inside', a.inside === null ? '—' : fmt(a.inside) + ' °C',
     a.inside === null ? 'hot-water zones only' : fmt(a.tMin) + '–' + fmt(a.tMax) + ' °C'],
    ['Avg outside', a.outside === null ? '—' : fmt(a.outside) + ' °C',
     a.outside === null ? '—' : fmt(a.oMin) + '–' + fmt(a.oMax) + ' °C'],
    ['Heating demand', fmt(a.hours) + ' zone-h',
     fmt(a.hours / Math.max(a.days, 1) / Math.max(a.zones, 1)) + ' h/day per zone'],
    ['Coverage', a.days + '/' + a.span, 'days · ' + a.zones + ' zones'],
    ['Readings', a.readings.toLocaleString(), '15-minute samples']
  ];
  el('tiles').innerHTML = tiles.map(function (t) {
    return '<div class="tile"><div class="k">' + esc(t[0]) + '</div><div class="v">' +
           esc(t[1]) + '</div><div class="n">' + esc(t[2]) + '</div></div>';
  }).join('');
}

function table(headers, rows, digits) {
  if (!rows.length) return '<p class="muted">No data in this range.</p>';
  var head = headers.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('');
  var body = rows.map(function (r) {
    return '<tr>' + r.map(function (c, i) {
      if (c === null || c === undefined) return '<td class="num muted">—</td>';
      if (typeof c === 'number') {
        var d = digits && digits[i] !== undefined ? digits[i] : 1;
        return '<td class="num">' + fmt(c, d) + '</td>';
      }
      return '<td>' + esc(c) + '</td>';
    }).join('') + '</tr>';
  }).join('');
  return '<div class="scroll"><table><thead><tr>' + head + '</tr></thead><tbody>' +
         body + '</tbody></table></div>';
}

function renderZoneTable() {
  var rows = selectedZones().map(function (z) {
    var s = R.summary[String(z.id)];
    var tSum = 0, tN = 0, tMin = null, tMax = null, hSum = 0, hN = 0, hours = 0, days = 0;
    for (var i = state.i0; i <= state.i1; i++) {
      if (s.tN[i]) {
        tSum += s.tSum[i]; tN += s.tN[i]; days++;
        if (s.tMin[i] !== null && (tMin === null || s.tMin[i] < tMin)) tMin = s.tMin[i];
        if (s.tMax[i] !== null && (tMax === null || s.tMax[i] > tMax)) tMax = s.tMax[i];
      }
      if (s.hN[i]) { hSum += s.hSum[i]; hN += s.hN[i]; }
      if (s.hours[i] !== null && s.hours[i] !== undefined) hours += s.hours[i];
    }
    return [z.name, z.pretty, tN, tN ? tSum / tN : null, tMin, tMax,
            hN ? hSum / hN : null, hours, days ? hours / days : null];
  });
  el('zone-table').innerHTML = table(
    ['Zone', 'Type', 'Readings', 'Avg °C', 'Min °C', 'Max °C',
     'Avg RH %', 'Heating h', 'h/day'], rows,
    [null, null, 0, 1, 1, 1, 1, 1, 1]);
}

function renderMonthTable() {
  var zones = selectedZones(), months = {}, order = [];
  for (var i = state.i0; i <= state.i1; i++) {
    var key = R.days[i].slice(0, 7);
    if (!months[key]) { months[key] = { tS: 0, tN: 0, oS: 0, oN: 0, oMin: null, h: 0, d: {} }; order.push(key); }
    var m = months[key];
    zones.forEach(function (z) {
      var s = R.summary[String(z.id)];
      if (s.tN[i] && z.type !== 'HOT_WATER') { m.tS += s.tSum[i]; m.tN += s.tN[i]; }
      if (s.tN[i]) m.d[i] = true;
      if (s.hours[i] !== null && s.hours[i] !== undefined) m.h += s.hours[i];
    });
    if (R.outside.n[i]) {
      m.oS += R.outside.sum[i]; m.oN += R.outside.n[i];
      if (R.outside.min[i] !== null && (m.oMin === null || R.outside.min[i] < m.oMin)) m.oMin = R.outside.min[i];
    }
  }
  var rows = order.map(function (k) {
    var m = months[k], days = Object.keys(m.d).length;
    return [k, m.tN ? m.tS / m.tN : null, m.oN ? m.oS / m.oN : null, m.oMin,
            m.h, days, days ? m.h / days : null];
  });
  el('month-table').innerHTML = table(
    ['Month', 'Avg inside °C', 'Avg outside °C', 'Min outside °C',
     'Heating h', 'Days', 'h/day'], rows,
    [null, 1, 1, 1, 1, 0, 1]);
}

// -- render -----------------------------------------------------------------

// An empty chart must explain itself rather than show a bare pair of axes.
function note(layout, text) {
  layout.annotations = [{ text: text, showarrow: false, x: 0.5, y: 0.5,
                          xref: 'paper', yref: 'paper', font: { size: 14 } }];
  ['xaxis', 'yaxis', 'xaxis2', 'yaxis2'].forEach(function (k) {
    if (layout[k]) layout[k].visible = false;
  });
}

function syncLabel(m) {
  var b = el('theme-toggle');
  if (!b) return;
  b.textContent = m === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
  b.setAttribute('aria-pressed', m === 'dark' ? 'true' : 'false');
}

function render() {
  var m = mode();
  syncLabel(m);

  var hasZones = state.zones.length > 0;
  el('report-body').style.display = hasZones ? '' : 'none';
  el('empty').style.display = hasZones ? 'none' : '';
  if (!hasZones) return;

  var span = state.i1 - state.i0 + 1;
  var useHourly = span <= R.hourlySpanLimit;
  var h0 = R.dayStart[state.i0];
  // +1: carry the line into the next day's first bucket so it reaches midnight.
  var h1 = Math.min(R.dayStart[state.i1 + 1] + 1, R.stamps.length);

  el('temp-hint').textContent = useHourly
    ? 'hourly, with target temperature' : 'daily means';
  el('hum-hint').textContent = useHourly ? 'hourly' : 'daily means';



  var tempLayout = clone(R.layout[m].temp);
  var humLayout = clone(R.layout[m].humidity);
  if (useHourly) {
    var window = dayWindow();
    setRangeOn(tempLayout, window);
    setRangeOn(humLayout, window);
  }
  Plotly.react(el('fig-temp'), tempTraces(useHourly, h0, h1, m), tempLayout, CONFIG);
  Plotly.react(el('fig-humidity'), humidityTraces(useHourly, h0, h1, m), humLayout, CONFIG);

  var choice = syncDemandZones();
  var heatZones = demandZones(choice);
  var heat = demandTrace(m, heatZones);
  var heatLayout = clone(R.layout[m].demand);
  if (!heat) note(heatLayout, 'No call-for-heat data in this period.');
  // Say what is being averaged: across several zones a single hot zone is
  // diluted by the quiet ones, so the peak reads lower than any zone's own.
  el('demand-hint').textContent = heatZones.length === 1
    ? 'mean boiler demand per hour of day — ' + heatZones[0].name
    : 'mean boiler demand per hour of day, averaged across ' +
      heatZones.length + ' zones';
  Plotly.react(el('fig-demand'), heat ? [heat] : [], heatLayout, CONFIG);

  var hours = hoursTraces(m);
  var hoursLayout = clone(R.layout[m].hours);
  if (!hours.traces.length) note(hoursLayout, 'No heating in this period.');
  hoursLayout.bargap = hours.nDays <= 120 ? 0.25 : 0.0;
  if (hours.hidden) {
    hoursLayout.annotations = [{
      text: hours.hidden + ' partly covered day' + (hours.hidden === 1 ? '' : 's') + ' hidden',
      showarrow: false, xref: 'paper', yref: 'paper', x: 1, y: 1.06,
      xanchor: 'right', yanchor: 'bottom', font: { size: 11 }
    }];
  }
  Plotly.react(el('fig-hours'), hours.traces, hoursLayout, CONFIG);

  renderTiles(aggregate());
  renderZoneTable();
  renderMonthTable();
}

// -- controls ---------------------------------------------------------------

function clampRange() {
  var last = R.days.length - 1;
  state.i0 = Math.max(0, Math.min(state.i0, last));
  state.i1 = Math.max(state.i0, Math.min(state.i1, last));
}

function syncInputs() {
  el('from').value = R.days[state.i0];
  el('to').value = R.days[state.i1];
  Array.prototype.forEach.call(el('presets').children, function (b) {
    b.setAttribute('aria-pressed', String(+b.dataset.days === state.preset));
  });
}

function applyPreset(days) {
  state.preset = days;
  var last = R.days.length - 1;
  state.i1 = last;
  state.i0 = days > 0 ? Math.max(0, last - days + 1) : 0;
  clampRange(); syncInputs(); render();
}

function indexOfDay(value, fallback) {
  var i = R.days.indexOf(value);
  if (i !== -1) return i;
  // Nearest day at or after the requested one, so a date outside the data still works.
  for (var k = 0; k < R.days.length; k++) if (R.days[k] >= value) return k;
  return fallback;
}

el('presets').addEventListener('click', function (ev) {
  var b = ev.target.closest('button[data-days]');
  if (b) applyPreset(+b.dataset.days);
});
['from', 'to'].forEach(function (id) {
  el(id).addEventListener('change', function () {
    state.preset = -1;
    state.i0 = indexOfDay(el('from').value, state.i0);
    state.i1 = indexOfDay(el('to').value, state.i1);
    if (state.i1 < state.i0) { var t = state.i0; state.i0 = state.i1; state.i1 = t; }
    clampRange(); syncInputs(); render();
  });
});
el('shift-back').addEventListener('click', function () {
  var span = state.i1 - state.i0 + 1;
  state.i0 -= span; state.i1 -= span;
  if (state.i0 < 0) { state.i0 = 0; state.i1 = Math.min(R.days.length - 1, span - 1); }
  clampRange(); syncInputs(); render();
});
el('shift-fwd').addEventListener('click', function () {
  var span = state.i1 - state.i0 + 1, last = R.days.length - 1;
  state.i0 += span; state.i1 += span;
  if (state.i1 > last) { state.i1 = last; state.i0 = Math.max(0, last - span + 1); }
  clampRange(); syncInputs(); render();
});
el('zones').addEventListener('change', function () {
  state.zones = Array.prototype.slice.call(document.querySelectorAll('.zone:checked'))
    .map(function (c) { return c.value; });
  render();
});
el('zones-all').addEventListener('click', function () {
  var boxes = document.querySelectorAll('.zone');
  var allOn = state.zones.length === boxes.length;
  Array.prototype.forEach.call(boxes, function (c) { c.checked = !allOn; });
  state.zones = allOn ? [] : R.zones.map(function (z) { return String(z.id); });
  el('zones-all').textContent = allOn ? 'select all' : 'clear all';
  render();
});
el('demand-zone').addEventListener('change', render);
el('theme-toggle').addEventListener('click', function () {
  var next = mode() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('tado-report-theme', next); } catch (e) {}
  render();
});
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
  var chosen = null;
  try { chosen = localStorage.getItem('tado-report-theme'); } catch (e) {}
  if (!chosen) render();
});

el('from').min = R.days[0]; el('from').max = R.days[R.days.length - 1];
el('to').min = R.days[0]; el('to').max = R.days[R.days.length - 1];
applyPreset(0);   // the whole period: a report should open on the overview
