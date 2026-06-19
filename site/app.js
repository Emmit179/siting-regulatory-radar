(() => {
  'use strict';

  const state = {
    dashboard: null,
    map: null,
    signals: [],
    coverage: new Map(),
    history: {},
    countyByFips: new Map(),
    topic: 'risk',
    selected: null,
    zoom: 1,
    panX: 0,
    panY: 0,
    dragging: false,
    dragStart: null,
    lastFocus: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const safeUrl = value => {
    try {
      const url = new URL(String(value || ''), window.location.href);
      return ['http:', 'https:'].includes(url.protocol) ? escapeHtml(url.href) : '#';
    } catch (_) { return '#'; }
  };
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const number = value => new Intl.NumberFormat('en-US').format(Number(value || 0));
  const pct = value => `${Math.round(Number(value || 0))}%`;
  const formatDate = value => {
    if (!value) return 'No dated activity';
    const date = new Date(value.length === 10 ? `${value}T12:00:00` : value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat('en-US', {month:'short', day:'numeric', year:'numeric'}).format(date);
  };
  const relativeDate = value => {
    if (!value) return 'undated';
    const date = new Date(value.length === 10 ? `${value}T12:00:00` : value);
    if (Number.isNaN(date.getTime())) return value;
    const days = Math.round((Date.now() - date.getTime()) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return '1 day ago';
    if (days < 30) return `${days} days ago`;
    if (days < 365) return `${Math.round(days / 30)} mo ago`;
    return `${Math.round(days / 365)} yr ago`;
  };

  const labels = {
    risk: 'Overall regulatory risk',
    solar: 'Solar regulatory risk',
    dataCenter: 'Data-center regulatory risk',
    bess: 'Battery-storage regulatory risk',
    wind: 'Wind regulatory risk',
  };
  const topicLabels = {
    solar: 'Solar', data_center: 'Data center', bess: 'Battery storage', wind: 'Wind', general_land_use: 'Land use',
  };
  const stageLabels = {
    mention: 'Mention', study: 'Study', staff_direction: 'Staff direction', drafting: 'Drafting', public_notice: 'Public notice',
    public_hearing: 'Public hearing', introduction: 'Introduced', adopted: 'Adopted', enforcement: 'Enforcement', rescinded: 'Rescinded',
  };
  const statusLabels = {
    unknown: 'Coverage unknown', no_current_signal: 'No current signal', watch: 'Watch', elevated: 'Elevated', high: 'High', critical: 'Critical',
  };
  const statusColors = {
    unknown: '#d3d4cf', no_current_signal: '#dbe7d9', watch: '#ead997', elevated: '#e7ae61', high: '#d96f50', critical: '#a93b45',
  };

  function statusFor(value, coverage) {
    value = Number(value || 0);
    if (coverage < 35 && value === 0) return 'unknown';
    if (value >= 78) return 'critical';
    if (value >= 58) return 'high';
    if (value >= 32) return 'elevated';
    if (value > 0) return 'watch';
    return 'no_current_signal';
  }

  function scoreColor(value, coverage) {
    return statusColors[statusFor(value, coverage)];
  }

  function statusBadge(status) {
    return `<span class="status-badge status-${escapeHtml(status)}">${escapeHtml(statusLabels[status] || status)}</span>`;
  }

  async function loadJSON(path) {
    const response = await fetch(path, {cache: 'no-store'});
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  }

  async function init() {
    try {
      const [dashboard, map, signalData, coverageData, historyData] = await Promise.all([
        loadJSON('data/dashboard.json'),
        loadJSON('data/map.json'),
        loadJSON('data/signals.json'),
        loadJSON('data/coverage.json'),
        loadJSON('data/history.json'),
      ]);
      state.dashboard = dashboard;
      state.map = map;
      state.signals = signalData.signals || [];
      state.history = historyData.history || {};
      dashboard.counties.forEach(county => state.countyByFips.set(county.fips, county));
      (coverageData.counties || []).forEach(county => state.coverage.set(county.fips, county));
      renderSummary();
      renderMap();
      renderFeed();
      renderTable();
      renderCoverage();
      wireControls();
      openHashCounty();
    } catch (error) {
      console.error(error);
      $('#map-loading').innerHTML = `<div class="empty-state"><b>Dashboard data could not load.</b><span>Run <code>update-now.bat</code>, then open this page through <code>start-dashboard.bat</code> rather than double-clicking index.html.</span></div>`;
      $('#signal-feed').innerHTML = `<div class="empty-state"><b>No data connection</b><span>${escapeHtml(error.message)}</span></div>`;
    }
  }

  function renderSummary() {
    const {stats, generatedAt} = state.dashboard;
    // $('#metric-high').textContent = number(stats.highRiskCount);
    // $('#metric-signals').textContent = number(stats.activeSignals);
    // $('#metric-coverage').textContent = `${Math.round((stats.coveredCount / Math.max(1, stats.countyCount)) * 100)}%`;
    // $('#metric-documents').textContent = number(stats.documents);
    $('#freshness-text').textContent = `Updated ${formatDate(generatedAt)}`;
    // $('#footer-date').textContent = `Data generated ${formatDate(generatedAt)}`;
    const actionable = state.dashboard.counties.filter(c => ['critical','high','elevated'].includes(c.status)).length;
    const unknown = state.dashboard.counties.filter(c => c.status === 'unknown').length;
    $('#map-summary').textContent = `${actionable} elevated or higher · ${unknown} coverage unknown`;
  }

  function renderMap() {
    const svg = $('#texas-map');
    svg.setAttribute('viewBox', state.map.viewBox);
    svg.innerHTML = '';
    const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    group.id = 'map-layer';
    state.map.counties.forEach(shape => {
      const county = state.countyByFips.get(shape.fips);
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', shape.path);
      path.setAttribute('class', 'county-path');
      path.setAttribute('data-fips', shape.fips);
      path.setAttribute('tabindex', '0');
      path.setAttribute('role', 'button');
      const value = county ? Number(county[state.topic] || 0) : 0;
      const coverage = county ? Number(county.coverage || 0) : 0;
      const status = statusFor(value, coverage);
      path.setAttribute('aria-label', `${shape.name} County: ${statusLabels[status]}, risk ${Math.round(value)} of 100, coverage ${Math.round(coverage)} percent`);
      path.style.fill = county ? scoreColor(value, coverage) : statusColors.unknown;
      path.addEventListener('pointerenter', event => showTooltip(event, shape.fips));
      path.addEventListener('pointermove', event => positionTooltip(event));
      path.addEventListener('pointerleave', hideTooltip);
      path.addEventListener('click', event => { event.stopPropagation(); openCounty(shape.fips); });
      path.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openCounty(shape.fips); }
      });
      group.appendChild(path);
    });
    svg.appendChild(group);
    applyMapTransform();
    $('#map-loading').hidden = true;
  }

  function updateMapColors() {
    $$('.county-path').forEach(path => {
      const county = state.countyByFips.get(path.dataset.fips);
      const value = county ? Number(county[state.topic] || 0) : 0;
      const coverage = county ? Number(county.coverage || 0) : 0;
      const status = statusFor(value, coverage);
      path.style.fill = county ? scoreColor(value, coverage) : statusColors.unknown;
      path.setAttribute('aria-label', `${county?.name || 'Unknown'} County: ${statusLabels[status]}, risk ${Math.round(value)} of 100, coverage ${Math.round(coverage)} percent`);
    });
    $('#map-title').textContent = labels[state.topic];
  }

  function showTooltip(event, fips) {
    const county = state.countyByFips.get(fips);
    if (!county) return;
    const value = county[state.topic] || 0;
    const status = statusFor(value, county.coverage);
    const tooltip = $('#map-tooltip');
    tooltip.innerHTML = `<strong>${escapeHtml(county.name)} County</strong><span>${escapeHtml(statusLabels[status])} · ${Math.round(value)}/100<br>Coverage ${Math.round(county.coverage)}%</span>`;
    tooltip.hidden = false;
    positionTooltip(event);
  }

  function positionTooltip(event) {
    const stage = $('#map-stage');
    const tooltip = $('#map-tooltip');
    const rect = stage.getBoundingClientRect();
    let x = event.clientX - rect.left + 14;
    let y = event.clientY - rect.top + 14;
    if (x + tooltip.offsetWidth > rect.width - 8) x -= tooltip.offsetWidth + 28;
    if (y + tooltip.offsetHeight > rect.height - 8) y -= tooltip.offsetHeight + 28;
    tooltip.style.left = `${Math.max(8, x)}px`;
    tooltip.style.top = `${Math.max(8, y)}px`;
  }

  function hideTooltip() { $('#map-tooltip').hidden = true; }

  function applyMapTransform() {
    const layer = $('#map-layer');
    if (layer) layer.setAttribute('transform', `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
  }

  function zoomMap(delta) {
    const old = state.zoom;
    state.zoom = clamp(state.zoom + delta, 1, 4.5);
    if (state.zoom === 1) { state.panX = 0; state.panY = 0; }
    else {
      const centerX = 460, centerY = 410;
      state.panX = centerX - (centerX - state.panX) * (state.zoom / old);
      state.panY = centerY - (centerY - state.panY) * (state.zoom / old);
    }
    applyMapTransform();
  }

  function resetMap() { state.zoom = 1; state.panX = 0; state.panY = 0; applyMapTransform(); }

  function renderFeed() {
    const filter = $('#feed-topic')?.value || 'all';
    const signals = state.signals.filter(signal => filter === 'all' || signal.topic === filter).slice(0, 30);
    const root = $('#signal-feed');
    if (!signals.length) {
      root.innerHTML = `<div class="empty-state"><b>No found signals yet.</b><span>The first update will populate this feed only when an official record contains a grounded regulatory indicator.</span></div>`;
      return;
    }
    root.innerHTML = signals.map(signal => `
      <article class="signal-card" data-url="${safeUrl(signal.sourceUrl)}" tabindex="0" role="link">
        <div class="signal-meta"><span class="signal-badge">${escapeHtml(topicLabels[signal.topic] || signal.topic)} · ${escapeHtml(stageLabels[signal.stage] || signal.stage)}</span><span class="signal-date">${escapeHtml(relativeDate(signal.meetingDate || signal.firstSeen))}</span></div>
        <h3>${escapeHtml(signal.title)}</h3>
        <p>${escapeHtml(signal.summary)}</p>
        <div class="signal-county"><span>${escapeHtml(signal.county)} County</span><span>Risk ${Math.round(signal.risk)}</span></div>
      </article>`).join('');
    $$('.signal-card', root).forEach(card => {
      const open = () => window.open(card.dataset.url, '_blank', 'noopener,noreferrer');
      card.addEventListener('click', open);
      card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
    });
  }

  function tableCounties() {
    const filter = $('#status-filter')?.value || 'all';
    let counties = [...state.dashboard.counties];
    if (filter === 'actionable') counties = counties.filter(c => ['elevated','high','critical'].includes(c.status));
    if (filter === 'unknown') counties = counties.filter(c => c.status === 'unknown');
    if (filter === 'signals') counties = counties.filter(c => c.signalCount > 0);
    return counties.sort((a, b) => b.risk - a.risk || b.signalCount - a.signalCount || a.name.localeCompare(b.name));
  }

  function miniScore(value) {
    const status = statusFor(value, 100);
    return `<span class="risk-number">${Math.round(value || 0)}</span><span class="mini-bar"><i style="width:${clamp(value || 0,0,100)}%;background:${statusColors[status]}"></i></span>`;
  }

  function renderTable() {
    const root = $('#county-table');
    root.innerHTML = tableCounties().map(county => `
      <tr data-fips="${escapeHtml(county.fips)}">
        <td class="county-name-cell"><strong>${escapeHtml(county.name)} County</strong><small>${county.signalCount} active signal${county.signalCount === 1 ? '' : 's'}</small></td>
        <td>${statusBadge(county.status)}</td>
        <td>${miniScore(county.risk)}</td>
        <td>${miniScore(county.solar)}</td>
        <td>${miniScore(county.dataCenter)}</td>
        <td>${miniScore(county.bess)}</td>
        <td>${Math.round(county.coverage)}%</td>
        <td>${escapeHtml(county.latestActivity ? formatDate(county.latestActivity) : '—')}</td>
        <td><button class="row-open" type="button" aria-label="Open ${escapeHtml(county.name)} County">→</button></td>
      </tr>`).join('');
    $$('tr[data-fips]', root).forEach(row => {
      row.addEventListener('click', () => openCounty(row.dataset.fips));
    });
  }

  function sparkline(points) {
    if (!points || points.length < 2) return `<div class="empty-state"><span>Trend history begins after the first daily snapshots.</span></div>`;
    const width = 500, height = 80, pad = 5;
    const values = points.map(point => Number(point.risk || 0));
    const max = Math.max(20, ...values);
    const coords = values.map((value, index) => [
      pad + (index / Math.max(1, values.length - 1)) * (width - pad * 2),
      height - pad - (value / max) * (height - pad * 2),
    ]);
    const line = coords.map((point, index) => `${index ? 'L' : 'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(' ');
    const area = `${line} L${coords.at(-1)[0]},${height} L${coords[0][0]},${height} Z`;
    return `<svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="180-day risk trend"><path class="area" d="${area}"></path><path d="${line}"></path></svg>`;
  }

  function topicRiskRow(label, value, coverage) {
    const status = statusFor(value, coverage);
    return `<div class="topic-risk"><span>${escapeHtml(label)}</span><div class="track"><i style="width:${clamp(value,0,100)}%;background:${statusColors[status]}"></i></div><strong>${Math.round(value)}</strong></div>`;
  }

  function renderEvidence(signals) {
    if (!signals.length) return `<div class="empty-state"><b>No current signals.</b><span>This is not a “safe” label. Review coverage and official sources below.</span></div>`;
    return signals.map(signal => `
      <article class="evidence-card">
        <div class="evidence-top"><span class="signal-badge">${escapeHtml(topicLabels[signal.topic] || signal.topic)} · ${escapeHtml(stageLabels[signal.stage] || signal.stage)}</span><span class="signal-date">Risk ${Math.round(signal.risk)} · ${Math.round(signal.confidence)}% confidence</span></div>
        <h4>${escapeHtml(signal.title)}</h4>
        <p>${escapeHtml(signal.summary)}</p>
        <blockquote>“${escapeHtml(signal.quote)}”</blockquote>
        ${signal.authorityCaveat ? `<p><strong>Authority caveat:</strong> ${escapeHtml(signal.authorityCaveat)}</p>` : ''}
        <div class="evidence-actions"><a href="${safeUrl(signal.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open official record ↗</a><small>${escapeHtml(formatDate(signal.meetingDate || signal.firstSeen))}</small></div>
      </article>`).join('');
  }

  function renderSources(coverage) {
    const sources = coverage?.sources || [];
    if (!sources.length) return `<div class="empty-state"><span>No substantive source has been confirmed yet. The next discovery run will retry the official site.</span></div>`;
    return sources.map(source => `
      <div class="source-row">
        <div><a href="${safeUrl(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || source.type)} ↗</a><br><small>${source.lastSuccess ? `last success ${relativeDate(source.lastSuccess)}` : 'not yet successfully crawled'}${source.lastError ? ` · ${escapeHtml(source.lastError.slice(0,120))}` : ''}</small></div>
        <i class="source-health ${source.failureCount >= 3 ? 'bad' : ''}" title="${source.failureCount >= 3 ? 'Repeated failures' : 'Healthy or pending'}"></i>
      </div>`).join('');
  }

  function openCounty(fips, updateHash = true) {
    const county = state.countyByFips.get(fips);
    if (!county) return;
    if (!state.selected) state.lastFocus = document.activeElement;
    state.selected = fips;
    const signals = state.signals.filter(signal => signal.countyFips === fips).sort((a,b) => b.risk - a.risk);
    const coverage = state.coverage.get(fips);
    const history = state.history[fips] || [];
    const status = county.status;
    const content = $('#drawer-content');
    content.innerHTML = `
      <header class="drawer-hero">
        <span class="kicker">County breakdown · FIPS ${escapeHtml(fips)}</span>
        <h2 id="drawer-title">${escapeHtml(county.name)} County</h2>
        <div class="drawer-risk-row">
          <div class="risk-orb" style="--orb-value:${clamp(county.risk,0,100)};--orb-color:${statusColors[status]}"><span><strong>${Math.round(county.risk)}</strong><small>overall risk</small></span></div>
          <div class="drawer-summary">${statusBadge(status)}<p>${county.signalCount ? `${county.signalCount} active indicator${county.signalCount === 1 ? '' : 's'}.` : 'No current indicators.'}</p><p>Coverage ${Math.round(county.coverage)}% · Confidence ${Math.round(county.confidence)}%</p><p>Latest activity: ${escapeHtml(county.latestActivity ? formatDate(county.latestActivity) : 'not yet observed')}</p></div>
        </div>
      </header>
      <div class="drawer-body">
        <section class="drawer-section">
          <div class="drawer-section-title"><h3>Risk by topic</h3><small>0–100 research priority</small></div>
          ${topicRiskRow('Utility-scale solar', county.solar, county.coverage)}
          ${topicRiskRow('Data centers', county.dataCenter, county.coverage)}
          ${topicRiskRow('Battery storage', county.bess, county.coverage)}
          ${topicRiskRow('Wind facilities', county.wind, county.coverage)}
        </section>
        <section class="drawer-section">
          <div class="drawer-section-title"><h3>Monitoring integrity</h3></div>
          <div class="audit-grid">
            <div class="audit-card"><span>Coverage</span><strong>${Math.round(county.coverage)}%</strong></div>
            <div class="audit-card"><span>Sources</span><strong>${county.sourceCount}</strong></div>
            <div class="audit-card"><span>Documents</span><strong>${number(county.documents)}</strong></div>

          </div>
        </section>
        <section class="drawer-section">
          <div class="drawer-section-title"><h3>Document ledger</h3><small>${signals.length} active</small></div>
          ${renderEvidence(signals)}
        </section>

        <section class="drawer-section">
          <div class="drawer-section-title"><h3>Official sources</h3>${county.officialUrl ? `<a href="${safeUrl(county.officialUrl)}" target="_blank" rel="noopener noreferrer"><small>County website ↗</small></a>` : '<small>site unresolved</small>'}</div>
          ${renderSources(coverage)}
        </section>
        <div class="drawer-disclaimer">These indicators summarize official public records for research triage. They do not establish secret intent, predict an outcome, provide legal advice, or determine whether the county possesses legal authority.</div>
      </div>`;
    $('#drawer-backdrop').hidden = false;
    $('#county-drawer').removeAttribute('inert');
    $('#county-drawer').classList.add('open');
    $('#county-drawer').setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    $$('.county-path').forEach(path => path.classList.toggle('selected', path.dataset.fips === fips));
    if (updateHash) historyReplace(`#county=${fips}`);
    setTimeout(() => $('#drawer-close').focus(), 50);
  }

  function closeCounty(updateHash = true) {
    state.selected = null;
    const drawer = $('#county-drawer');
    const returnFocus = state.lastFocus;
    state.lastFocus = null;
    if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
    if (drawer.contains(document.activeElement)) document.activeElement.blur();
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    drawer.setAttribute('inert', '');
    $('#drawer-backdrop').hidden = true;
    document.body.style.overflow = '';
    $$('.county-path').forEach(path => path.classList.remove('selected'));
    if (updateHash) historyReplace(location.pathname + location.search);
  }

  function historyReplace(value) {
    try { window.history.replaceState(null, '', value); } catch (_) { /* static hosting fallback */ }
  }

  function openHashCounty() {
    const match = location.hash.match(/county=(48\d{3})/);
    if (match) openCounty(match[1], false);
  }

  function renderCoverage() {
    const counties = [...state.coverage.values()].sort((a,b) => a.coverage - b.coverage || a.name.localeCompare(b.name));
    const resolved = counties.filter(c => c.siteStatus === 'resolved').length;
    const monitored = counties.filter(c => c.coverage >= 35).length;
    const failing = counties.filter(c => (c.sources || []).some(s => s.failureCount >= 3)).length;
    $('#coverage-summary').innerHTML = `
      <article><strong>${resolved}</strong><span>official sites resolved</span></article>
      <article><strong>${monitored}</strong><span>counties monitored</span></article>
      <article><strong>${counties.length - monitored}</strong><span>coverage below threshold</span></article>
      <article><strong>${failing}</strong><span>with repeated source failures</span></article>`;
    $('#coverage-list').innerHTML = counties.map(county => `
      <div class="coverage-row"><strong>${escapeHtml(county.name)}</strong><div class="coverage-track"><i style="width:${clamp(county.coverage,0,100)}%"></i></div><span>${Math.round(county.coverage)}%</span><span>${county.sources.length} sources</span></div>`).join('');
  }

  function searchCounties(query) {
    const root = $('#search-results');
    query = query.trim().toLowerCase();
    if (!query) { root.hidden = true; return; }
    const results = state.dashboard.counties.filter(c => c.name.toLowerCase().includes(query)).slice(0, 10);
    root.innerHTML = results.length ? results.map(county => `<button class="search-result" data-fips="${county.fips}" type="button"><span>${escapeHtml(county.name)} County</span><small>${escapeHtml(statusLabels[county.status])}</small></button>`).join('') : `<div class="empty-state"><span>No Texas county matched.</span></div>`;
    root.hidden = false;
    $$('.search-result', root).forEach(button => button.addEventListener('click', () => {
      $('#county-search').value = `${state.countyByFips.get(button.dataset.fips).name} County`;
      root.hidden = true;
      openCounty(button.dataset.fips);
    }));
  }

  function wireControls() {
    $$('.topic').forEach(button => button.addEventListener('click', () => {
      $$('.topic').forEach(item => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      state.topic = button.dataset.topic;
      updateMapColors();
    }));
    $('#feed-topic').addEventListener('change', renderFeed);
    $('#status-filter').addEventListener('change', renderTable);
    $('#county-search').addEventListener('input', event => searchCounties(event.target.value));
    $('#county-search').addEventListener('keydown', event => {
      if (event.key === 'Escape') $('#search-results').hidden = true;
      if (event.key === 'Enter') {
        const first = $('.search-result', $('#search-results'));
        if (first) first.click();
      }
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('.search-wrap')) $('#search-results').hidden = true;
    });
    $('#zoom-in').addEventListener('click', () => zoomMap(.35));
    $('#zoom-out').addEventListener('click', () => zoomMap(-.35));
    $('#zoom-reset').addEventListener('click', resetMap);
    const stage = $('#map-stage');
    stage.addEventListener('wheel', event => { event.preventDefault(); zoomMap(event.deltaY < 0 ? .25 : -.25); }, {passive:false});
    stage.addEventListener('pointerdown', event => {
      if (event.target.classList.contains('county-path') && state.zoom === 1) return;
      state.dragging = true;
      state.dragStart = {x:event.clientX, y:event.clientY, panX:state.panX, panY:state.panY};
      stage.classList.add('dragging');
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener('pointermove', event => {
      if (!state.dragging) return;
      const rect = stage.getBoundingClientRect();
      const sx = 920 / rect.width / state.zoom;
      const sy = 820 / rect.height / state.zoom;
      state.panX = state.dragStart.panX + (event.clientX - state.dragStart.x) * sx;
      state.panY = state.dragStart.panY + (event.clientY - state.dragStart.y) * sy;
      applyMapTransform();
    });
    const endDrag = () => { state.dragging = false; stage.classList.remove('dragging'); };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
    $('#drawer-close').addEventListener('click', () => closeCounty());
    $('#drawer-backdrop').addEventListener('click', () => closeCounty());
    // $('#methodology-button').addEventListener('click', () => $('#methodology-modal').showModal());
    $('#coverage-button').addEventListener('click', () => $('#coverage-modal').showModal());
    $$('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
    $$('dialog').forEach(dialog => dialog.addEventListener('click', event => {
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    }));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && state.selected) closeCounty();
      if (event.key === 'Tab' && state.selected) {
        const drawer = $('#county-drawer');
        const focusable = $$('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])', drawer)
          .filter(element => !element.hasAttribute('inert'));
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    window.addEventListener('hashchange', openHashCounty);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
