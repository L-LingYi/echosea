import {
  GATEWAY_MODE_LABELS,
  SLOT_LABELS,
  STORE_LABELS,
  api,
  badge,
  el,
  formatCompactNum,
  labelOf,
  replaceChildren,
} from './api.js';

let USAGE_SERIES_CACHE = { daily: [], hourly: [] };
let USAGE_GRANULARITY = 'daily';

export function switchUsageGranularity(gran) {
  USAGE_GRANULARITY = gran === 'hourly' ? 'hourly' : 'daily';
  document.querySelectorAll('[data-ugran]').forEach(b => b.classList.toggle('active', b.dataset.ugran === USAGE_GRANULARITY));
  renderUsageChart();
}

export function selectUsageBar(idx) {
  const items = USAGE_SERIES_CACHE[USAGE_GRANULARITY] || [];
  const item = items[idx];
  if (!item) return;
  document.querySelectorAll('#usageBars .usage-bar-col').forEach((elCol, i) => elCol.classList.toggle('active', i === idx));
  const title = USAGE_GRANULARITY === 'hourly' ? (item.hour || item.label) : (item.date || item.label);
  const summaryNodes = [
    el('strong', {}, title),
    ` · 总消耗 `,
    el('strong', {}, Number(item.tokens || 0).toLocaleString()),
    ` Token（输入 ${Number(item.prompt_tokens || 0).toLocaleString()} / 输出 ${Number(item.completion_tokens || 0).toLocaleString()}）· 调用 ${Number(item.calls || 0)} 次`,
  ];
  if (item.tts_chars) {
    summaryNodes.push(' · 语音 ', el('strong', {}, Number(item.tts_chars).toLocaleString()), ' 字');
  }
  replaceChildren(
    'usageChartMeta',
    el('span', {}, ...summaryNodes),
    el('span', {}, '单位：Token'),
  );
}

export function renderUsageChart() {
  const items = USAGE_SERIES_CACHE[USAGE_GRANULARITY] || [];
  if (!items.length) {
    replaceChildren('usageChartMeta', el('span', {}, '暂无消耗趋势数据'), el('span', {}, '单位：Token'));
    replaceChildren('usageBars', el('div', { className: 'empty-state', style: 'width:100%;' }, '暂无统计数据'));
    return;
  }
  const maxTok = Math.max(1, ...items.map(x => Number(x.tokens || 0)));
  const totalTok = items.reduce((acc, x) => acc + Number(x.tokens || 0), 0);
  const totalCalls = items.reduce((acc, x) => acc + Number(x.calls || 0), 0);
  const totalTts = items.reduce((acc, x) => acc + Number(x.tts_chars || 0), 0);
  const winLabel = USAGE_GRANULARITY === 'hourly' ? '最近 24 小时' : `近 ${items.length} 天`;
  const leftNodes = [
    `${winLabel}累计 `,
    el('strong', {}, totalTok.toLocaleString()),
    ` Token · 调用 ${totalCalls.toLocaleString()} 次`,
  ];
  if (totalTts) leftNodes.push(' · 语音 ', el('strong', {}, totalTts.toLocaleString()), ' 字');
  leftNodes.push('（悬停或点击柱条查看明细）');
  replaceChildren(
    'usageChartMeta',
    el('span', {}, ...leftNodes),
    el('span', {}, `峰值 ${maxTok.toLocaleString()} Token`),
  );

  const isHourly = USAGE_GRANULARITY === 'hourly';
  const cols = items.map((item, idx) => {
    const val = Number(item.tokens || 0);
    const pct = val > 0 ? Math.max(4, Math.round((val / maxTok) * 100)) : 0;
    const shortLbl = isHourly
      ? ((idx % 3 === 0 || idx === items.length - 1) ? item.label.slice(0, 2) : '')
      : (items.length > 14 ? ((idx % 5 === 0 || idx === items.length - 1) ? item.label : '') : item.label);
    const showVal = isHourly
      ? (val === maxTok || idx % 3 === 0)
      : (items.length <= 10 || val === maxTok || idx % 5 === 0 || idx === items.length - 1);
    const topVal = val > 0 && showVal ? formatCompactNum(val) : '';
    return el(
      'div',
      {
        className: `usage-bar-col${val === 0 ? ' zero' : ''}`,
        title: `${item.hour || item.date}：${val.toLocaleString()} Token`,
        onMouseenter: () => selectUsageBar(idx),
        onClick: () => selectUsageBar(idx),
      },
      el('div', { className: 'usage-bar-val' }, topVal),
      el('div', { className: 'usage-bar-track' }, el('div', { className: 'usage-bar-fill', style: `height:${val > 0 ? pct : 3}%` })),
      el('div', { className: 'usage-bar-lbl' }, shortLbl),
    );
  });
  replaceChildren('usageBars', cols);
}

export async function loadDashboard() {
  const tzOffset = -new Date().getTimezoneOffset();
  const d = await api(`/api/dashboard/overview?days=7&tz_offset_minutes=${tzOffset}`);
  USAGE_SERIES_CACHE = d.usage_series || { daily: [], hourly: [] };
  const cachePct = Math.min(100, Number(d.tokens.prompt_cache_hit_rate_pct || 0));

  const card1 = el(
    'div',
    { className: 'card kpi-card' },
    el('div', { className: 'kpi-top' }, el('span', { className: 'kpi-label' }, '今日 Token 消耗'), el('span', { className: 'kpi-icon' }, '🔢')),
    el('div', { className: 'kpi-val' }, Number(d.tokens.today_total_tokens || 0).toLocaleString()),
    el('div', { className: 'progress-track' }, el('div', { className: 'progress-fill', style: `width:${cachePct.toFixed(1)}%;background:#4f46e5` })),
    el('div', { className: 'kpi-sub' }, `近 ${d.window_days} 天总计 `, el('strong', {}, Number(d.tokens.total_tokens || 0).toLocaleString()), ` · 缓存命中 ${cachePct}%`),
  );

  const latencySubs = [];
  if (d.tokens.latency_excluded_calls) {
    latencySubs.push(el('div', { className: 'kpi-sub' }, `已排除 ${Number(d.tokens.latency_excluded_calls)} 条历史耗时样本`));
  }
  const slowBadge = d.tokens.slow_response_alerts ? badge(`慢响应告警 ${d.tokens.slow_response_alerts}`, 'warn') : null;
  const card2 = el(
    'div',
    { className: 'card kpi-card' },
    el('div', { className: 'kpi-top' }, el('span', { className: 'kpi-label' }, '生产响应耗时'), el('span', { className: 'kpi-icon' }, '⚡')),
    el('div', { className: 'kpi-val' }, `${d.tokens.avg_ttft_ms}ms`),
    ...latencySubs,
    el('div', { className: 'kpi-sub' }, `P95 ${d.tokens.p95_ttft_ms}ms · 缓存 `, el('strong', {}, `${d.tokens.prompt_cache_hit_rate_pct}%`), ' ', slowBadge),
  );

  const card3 = el(
    'div',
    { className: 'card kpi-card' },
    el('div', { className: 'kpi-top' }, el('span', { className: 'kpi-label' }, '门卫放行率'), el('span', { className: 'kpi-icon' }, '🛡️')),
    el('div', { className: 'kpi-val' }, `${d.gatekeeper.pass_rate_pct}%`),
    el('div', { className: 'kpi-sub' }, `近 7 天累计处理 ${d.gatekeeper.total_messages} 条消息`),
  );

  const card4 = el(
    'div',
    { className: 'card kpi-card' },
    el('div', { className: 'kpi-top' }, el('span', { className: 'kpi-label' }, '语音合成字符'), el('span', { className: 'kpi-icon' }, '🎙️')),
    el('div', { className: 'kpi-val' }, `${Number(d.tts.today_chars || 0).toLocaleString()} 字`),
    el(
      'div',
      { className: 'kpi-sub' },
      `近 ${d.window_days} 天 `,
      el('strong', {}, `${Number(d.tts.total_chars || 0).toLocaleString()} 字`),
      ` · 合成 ${d.tts.synthesized_count} 次 (成功率 ${d.tts.success_rate_pct}% / 受理 ${d.tts.onebot_accept_rate_pct ?? '-'}%)`,
    ),
  );
  replaceChildren('kpiRow', card1, card2, card3, card4);

  const gw = d.health.gateway;
  replaceChildren(
    'healthBox',
    el(
      'div',
      { className: 'pill-list', style: 'margin-bottom:12px;' },
      badge(`● ${gw.gateway}（${labelOf(GATEWAY_MODE_LABELS, gw.mode)} · ${gw.connected ? '已连接' : '未连接'}）`, gw.connected ? 'ok' : 'warn'),
      badge(`状态库：${labelOf(STORE_LABELS, d.health.state_store)}`, 'info'),
      badge(`插件：${d.health.plugins.length} 个`, 'purple'),
    ),
    el(
      'div',
      { style: 'font-size:13px;color:var(--ink2);' },
      '已绑定 QQ 熟人卡 ', el('strong', {}, d.counts.qq_identities), ' 位 · 已启用表情包 ',
      el('strong', {}, d.counts.stickers_approved), ' 张 · 出站投递 ',
      el('strong', {}, d.counts.outbox_sent), ' 条',
    ),
  );

  const gk = d.gatekeeper.breakdown || {};
  const tot = Math.max(1, d.gatekeeper.total_messages);
  const gkItems = [
    ['direct_pass', '直接放行（@ / 引用 / 直呼小名）', '#4f46e5'],
    ['soft_entry', '降权软切入（主动接梗）', '#10b981'],
    ['silent', '静默旁听（沉淀边缘记忆）', '#64748b'],
    ['blocked_recall', '撤回即时拦截', '#ef4444'],
  ];
  replaceChildren(
    'gatekeeperBox',
    gkItems.map(([k, label, col]) => {
      const cnt = Number(gk[k] || 0);
      const pct = ((cnt / tot) * 100).toFixed(1);
      return el(
        'div',
        { className: 'bar-row' },
        el('div', { className: 'bar-meta' }, el('span', {}, label), el('span', {}, el('strong', {}, cnt), ` (${pct}%)`)),
        el('div', { className: 'progress-track' }, el('div', { className: 'progress-fill', style: `width:${pct}%;background:${col}` })),
      );
    }),
  );

  renderUsageChart();

  if (!d.by_slot.length) {
    replaceChildren('slotTable', el('tr', {}, el('td', { colSpan: 5, className: 'empty-state' }, '暂无生产调用数据')));
  } else {
    replaceChildren(
      'slotTable',
      d.by_slot.map(s => (s.unit === 'chars'
        ? el(
          'tr',
          {},
          el('td', {}, badge(labelOf(SLOT_LABELS, s.slot), 'purple')),
          el('td', {}, s.calls),
          el('td', {}, '-'),
          el('td', {}, '-'),
          el('td', {}, el('strong', {}, `${Number(s.chars || 0).toLocaleString()} 字符`)),
        )
        : el(
          'tr',
          {},
          el('td', {}, badge(labelOf(SLOT_LABELS, s.slot), 'info')),
          el('td', {}, s.calls),
          el('td', {}, Number(s.prompt_tokens || 0).toLocaleString()),
          el('td', {}, Number(s.completion_tokens || 0).toLocaleString()),
          el('td', {}, el('strong', {}, `${Number(s.tokens || 0).toLocaleString()} Token`)),
        ))),
    );
  }
}
