/**
 * 南方电网电费卡片（HACS 分享版 v1.5.2，基于 v49 自适应版）
 * 读取集成生成的每日电费缓存，可查询全部历史月份；
 * 日历配色对齐南网小程序的 6 档绝对用电量色阶；
 * 底部「详细信息」按钮直达查询页；户号从卡片配置自动推导，不含硬编码账号。
 * 年度累计随翻页年份变化（翻到 2025 年即显示 2025 年累计）。
 * v1.5.2：手机窄屏下 7 列日历按卡片宽度自适应，不再被裁切；
 *         年度累计在窄卡片上分两行完整显示。
 * v1.5.3：兼容被改成中文 / 拼音的 entity_id —— 按注册表原名与集成属性
 *         自动定位数据源，卡片配置里的旧 ID 失效时也能自动回退。
 */
// Same-origin relative URL: works from any HA address, contains no private IP.
// The HACS build replaces this with /csg_stat/csg_daily_cost.html.
const DEFAULT_DETAIL_URL = '/csg_stat/csg_daily_cost.html';

// 集成创建实体时使用的英文后缀（对应 const.py 里的 SUFFIX_*）。
// entity_id 允许被用户改成拼音 / 中文，所以不能只靠拼 ID 找实体。
const CSG_SUFFIXES = [
  'balance', 'arrears', 'yesterday_kwh',
  'latest_day_kwh', 'latest_day_cost',
  'this_year_total_usage', 'this_year_total_cost',
  'this_month_total_usage', 'this_month_total_cost',
  'current_ladder', 'current_ladder_remaining_kwh', 'current_ladder_tariff',
  'last_year_total_usage', 'last_year_total_cost',
  'last_month_total_usage', 'last_month_total_cost'
];
console.log('[csg-power-card] loading...');
const CSGPowerCard = class extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._built) { this._built = true; this._build(); }
    // 翻页状态不重置
    this._update();
  }
  setConfig(config) { this._config = config; }
  getCardSize() { return this._expanded ? 9 : 4; }

  // Account number is derived from the configured entity (or given explicitly),
  // so the same card works for every installation without editing the source.

  // ---- v1.5.3 实体自动定位 -------------------------------------------
  // 按「注册表原名 / 显示名 / ID 英文后缀」和「集成独有属性 + device_class」
  // 建立 英文后缀 -> 实际 entity_id 的索引；多账户时优先同一设备 / 同一户号。
  _suffixIndex() {
    const states = (this._hass && this._hass.states) || {};
    if (this._idxStates === states) return this._idxCache;
    const reg = (this._hass && this._hass.entities) || {};
    const cfg = this._config || {};
    const eid = cfg.entity || '';
    const device = reg[eid] && reg[eid].device_id;
    const acct = (String(eid).match(/\d{6,}/) || [''])[0];
    const best = {};
    const put = (suffix, id, base) => {
      if (!id || !states[id]) return;
      let score = base;
      if (device && reg[id] && reg[id].device_id === device) score += 8;
      if (acct && id.indexOf(acct) >= 0) score += 4;
      if (!best[suffix] || score > best[suffix].score) best[suffix] = { id, score };
    };
    // 1) 正常安装：ID / 注册表原名 / 显示名里就带英文后缀，最精确
    for (const id of Object.keys(states)) {
      if (id.indexOf('sensor.') !== 0) continue;
      const a = states[id].attributes || {};
      const e = reg[id] || {};
      const names = [e.original_name, e.name, a.friendly_name, id]
        .filter(Boolean).map(String);
      for (const suffix of CSG_SUFFIXES) {
        if (names.some(n => n === suffix || n.endsWith('_' + suffix) || n.endsWith(' ' + suffix))) {
          put(suffix, id, 30);
          break;
        }
      }
    }
    // 2) 被改名成拼音 / 中文：靠集成写进属性的独有明细数组来认
    for (const id of Object.keys(states)) {
      const a = states[id].attributes || {};
      const isCost = a.device_class === 'monetary';
      if (Array.isArray(a.this_month_by_day))
        put(isCost ? 'this_month_total_cost' : 'this_month_total_usage', id, 20);
      if (Array.isArray(a.last_month_by_day))
        put(isCost ? 'last_month_total_cost' : 'last_month_total_usage', id, 20);
      if (Array.isArray(a.this_year_by_month) || Array.isArray(a.history_by_month))
        put('this_year_total_usage', id, 20);
      if (Array.isArray(a.last_year_by_month)) put('last_year_total_usage', id, 20);
      if (a.current_ladder_start_date) put('current_ladder', id, 20);
      if (a.latest_day_date) put(isCost ? 'latest_day_cost' : 'latest_day_kwh', id, 20);
    }
    const out = {};
    for (const k of Object.keys(best)) out[k] = best[k].id;
    this._idxStates = states;
    this._idxCache = out;
    return out;
  }

  // 该实体在集成里的角色（英文后缀），用来判断年度 / 上年视图。
  _roleOf(id) {
    const s = ((this._hass && this._hass.states) || {})[id];
    if (!s) return '';
    const a = s.attributes || {};
    const e = ((this._hass && this._hass.entities) || {})[id] || {};
    const names = [e.original_name, e.name, a.friendly_name, id]
      .filter(Boolean).map(String);
    for (const suffix of CSG_SUFFIXES) {
      if (names.some(n => n === suffix || n.endsWith('_' + suffix) || n.endsWith(' ' + suffix)))
        return suffix;
    }
    if (Array.isArray(a.this_year_by_month) || Array.isArray(a.history_by_month))
      return 'this_year_total_usage';
    if (Array.isArray(a.this_month_by_day))
      return a.device_class === 'monetary' ? 'this_month_total_cost' : 'this_month_total_usage';
    return '';
  }

  // 先按 v1.5.2 的老路径直取，取不到再走自动索引。
  _state(suffix) {
    const states = (this._hass && this._hass.states) || {};
    const direct = states[this._entityPrefix() + suffix];
    if (direct) return direct;
    const id = this._suffixIndex()[suffix];
    return id ? states[id] : undefined;
  }

  _entityPrefix() {
    const cfg = this._config || {};
    if (cfg.entity_prefix) return cfg.entity_prefix;
    if (cfg.account) return 'sensor.' + String(cfg.account).replace(/^sensor\./, '') + '_';
    const eid = cfg.entity || '';
    const m = eid.match(/^(sensor\..+?)_(?:this_year|last_year|this_month|last_month|latest_day|yesterday|current_ladder|balance|arrears)/);
    if (m) return m[1] + '_';
    // v1.5.3：ID 被改成拼音时，从同一账户里仍是英文 ID 的实体反推前缀
    const idx = this._suffixIndex();
    for (const suffix of Object.keys(idx)) {
      const id = idx[suffix];
      if (typeof id === 'string' && id.endsWith('_' + suffix))
        return id.slice(0, id.length - suffix.length);
    }
    const found = Object.keys((this._hass && this._hass.states) || {})
      .find(k => k.startsWith('sensor.') && k.endsWith('_this_year_total_usage'));
    return found ? found.slice(0, -'this_year_total_usage'.length) : 'sensor.';
  }

  // 南网小程序口径：按「绝对日用电量」分 6 档，不随当月最大/最小值浮动。
  // 默认阈值 5/10/15/20/25 度；卡片可配 tier_thresholds 覆盖（必须为 5 个数）。
  _csgLevel(kwh) {
    const cfg = this._config || {};
    const t = (Array.isArray(cfg.tier_thresholds) && cfg.tier_thresholds.length === 5)
      ? cfg.tier_thresholds.map(Number) : [5, 10, 15, 20, 25];
    const v = Number(kwh) || 0;
    for (let i = 0; i < t.length; i++) { if (v < t[i]) return i; }
    return t.length;
  }

  _build() {
    this.attachShadow({ mode: 'open' });
    this._expanded = false; // 默认折叠
    this._viewMonthOffset = 0; // 0=本月, -1=上月, +1=下月
    this._historyDataCache = {}; // 缓存历史月份数据
    
    this.shadowRoot.innerHTML = `
      <style>
        /* v49 自适应：所有内边距 / 字号 / 间距按卡片实际宽度等比缩放，
           任何宽度下 7 列日历都完整落在卡片内，不会被 overflow:hidden 裁掉。 */
        :host{
          display:block;width:100%;max-width:100%;min-width:0;
          --csg-w:420px; /* JS ResizeObserver 会用实测宽度覆盖 */
          --p:min(24px,calc(var(--csg-w)*.058));
          --p2:min(16px,calc(var(--csg-w)*.040));
          --g:min(16px,calc(var(--csg-w)*.038));
          --g4:min(4px,calc(var(--csg-w)*.010));
          --g8:min(8px,calc(var(--csg-w)*.020));
          --f9:min(9px,calc(var(--csg-w)*.022));
          --f10:min(10px,calc(var(--csg-w)*.0245));
          --f11:min(11px,calc(var(--csg-w)*.027));
          --f12:min(12px,calc(var(--csg-w)*.029));
          --f13:min(13px,calc(var(--csg-w)*.032));
          --f14:min(14px,calc(var(--csg-w)*.034));
          --f16:min(16px,calc(var(--csg-w)*.039));
          --f18:min(18px,calc(var(--csg-w)*.044));
          --f20:min(20px,calc(var(--csg-w)*.049));
          --f28:min(28px,calc(var(--csg-w)*.068));
          --rad:min(24px,calc(var(--csg-w)*.058));
          --rad2:min(16px,calc(var(--csg-w)*.039));
          --rad3:min(12px,calc(var(--csg-w)*.029));
          --rad4:min(8px,calc(var(--csg-w)*.020));
        }
        *{box-sizing:border-box;margin:0;padding:0;min-width:0}
        .card{border-radius:var(--rad);overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.12);
          transition:box-shadow .2s;font-family:-apple-system,'PingFang SC',sans-serif;
          background:linear-gradient(135deg,#1a237e,#283593);cursor:pointer;
          width:100%;max-width:100%}
        .card:hover{box-shadow:0 8px 30px rgba(0,0,0,.22)} /* 不再 scale，避免窄屏被裁 */

        /* 折叠状态 */
        .card.expanded{cursor:default}
        .collapsed-content{display:flex;align-items:center;justify-content:space-between;
          padding:var(--p2) var(--p);min-width:0}
        .collapsed-metrics{display:flex;gap:var(--g8);flex:1 1 0;min-width:0;justify-content:space-around}
        .collapsed-metric{text-align:center;flex:1 1 0;min-width:0}
        .collapsed-value{font-size:var(--f18);font-weight:600;color:white;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .collapsed-label{font-size:var(--f10);color:rgba(255,255,255,.5);margin-top:2px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .collapsed-icon{font-size:var(--f20);color:rgba(255,255,255,.6);display:block;margin:0 auto 4px}

        /* 展开内容 */
        .expanded-content{display:none}
        .card.expanded .collapsed-content{display:none}
        .card.expanded .expanded-content{display:block}

        .header{padding:var(--p) var(--p) var(--p2);color:white;position:relative;overflow:hidden}
        .header::before{content:'';position:absolute;right:0;top:-30px;
          width:min(120px,calc(var(--csg-w)*.30));height:min(120px,calc(var(--csg-w)*.30));
          background:rgba(255,255,255,.08);border-radius:50%}
        .header::after{content:'';position:absolute;right:var(--p2);bottom:-30px;
          width:min(80px,calc(var(--csg-w)*.20));height:min(80px,calc(var(--csg-w)*.20));
          background:rgba(255,255,255,.05);border-radius:50%}
        .title{display:flex;align-items:center;justify-content:space-between;gap:var(--g8);position:relative;z-index:1;min-width:0}
        .title-left{display:flex;align-items:center;gap:var(--g8);min-width:0}
        .icon{font-size:var(--f28);flex:0 0 auto}
        .title-text{font-size:var(--f16);font-weight:600;opacity:.95;white-space:nowrap}
        .subtitle{font-size:var(--f12);opacity:.7;margin-top:2px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .collapse-btn{font-size:var(--f20);color:rgba(255,255,255,.6);cursor:pointer;padding:var(--g4);flex:0 0 auto}
        .collapse-btn:hover{color:white}
        .main{padding:0 var(--p) var(--p);position:relative;z-index:1;min-width:0}

        /* 翻页按钮 */
        .page-nav{display:flex;gap:var(--g8);margin-top:var(--f12);justify-content:space-between;min-width:0}
        .page-btn{background:rgba(255,255,255,.1);border:none;border-radius:var(--rad4);
          padding:var(--g8) min(10px,calc(var(--csg-w)*.024));color:white;font-size:var(--f12);
          cursor:pointer;transition:background .2s;flex:1 1 0;min-width:0;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .page-btn:hover{background:rgba(255,255,255,.2)}
        .page-btn:disabled{opacity:.3;cursor:not-allowed}
        #current-month{flex:1 1 0;margin:0 var(--g4)}

        /* 双指标 */
        .metrics{display:flex;gap:var(--g);margin-bottom:var(--g);min-width:0}
        .metric{flex:1 1 0;min-width:0;background:rgba(255,255,255,.1);border-radius:var(--rad2);
          padding:var(--p2) min(6px,calc(var(--csg-w)*.015));text-align:center}
        .metric-label{font-size:var(--f11);color:rgba(255,255,255,.6);display:flex;
          align-items:center;justify-content:center;gap:min(6px,calc(var(--csg-w)*.015));white-space:nowrap}
        .metric-icon{font-size:var(--f14)}
        .metric-value{font-size:var(--f18);font-weight:600;color:white;margin-top:4px;
          text-align:center;display:block;white-space:normal;overflow-wrap:anywhere;line-height:1.2}
        /* 年累计：宽屏单行「3080度/2070元」，窄屏竖排两行，数字完整不截断 */
        #year-info{white-space:nowrap;letter-spacing:-.3px}
        .yi-sep{opacity:.55;margin:0 1px}
        :host([csg-narrow]) #year-info{font-size:clamp(11px,calc(var(--csg-w)*.036),15px);line-height:1.25}
        :host([csg-narrow]) #year-info .yi{display:block}
        :host([csg-narrow]) #year-info .yi-sep{display:none}
        .metric-unit{font-size:var(--f11);font-weight:400;opacity:.7;margin-left:1px}

        /* 对比 */
        .compare-row{display:flex;align-items:center;gap:var(--g8);margin-bottom:var(--g);
          font-size:var(--f13);color:rgba(255,255,255,.8);flex-wrap:wrap;min-width:0}
        .compare-tag{padding:var(--g4) min(10px,calc(var(--csg-w)*.024));border-radius:20px;
          font-size:var(--f11);font-weight:500;white-space:nowrap}
        .compare-tag.up{background:rgba(255,138,128,.2);color:#ff8a80}
        .compare-tag.down{background:rgba(105,240,174,.2);color:#69f0ae}

        /* 日历：minmax(0,1fr) 是关键，禁止格子把列撑宽 */
        .calendar-area{background:rgba(255,255,255,.08);border-radius:var(--rad2);
          padding:var(--p2);margin-top:var(--g8);min-width:0;overflow:hidden}
        .calendar-header{display:flex;justify-content:space-between;align-items:center;
          margin-bottom:var(--f12);min-width:0}
        .calendar-title{font-size:var(--f13);font-weight:600;color:rgba(255,255,255,.9)}
        .weekday-row{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:var(--g4);margin-bottom:var(--g4)}
        .weekday{font-size:var(--f10);color:rgba(255,255,255,.4);text-align:center;padding:var(--g4) 0}
        .calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:var(--g4)}
        .day-cell{aspect-ratio:1/1;min-width:0;min-height:0;border-radius:var(--rad4);
          display:flex;flex-direction:column;align-items:center;justify-content:center;
          padding:2px 1px;line-height:1.15;overflow:hidden}
        .day-cell.empty{background:transparent}
        .day-cell.future{background:rgba(255,255,255,.04);border:1px dashed rgba(255,255,255,.1)}
        .day-num{font-size:var(--f9);font-weight:500;color:rgba(255,255,255,.6);white-space:nowrap}
        .day-kwh{font-size:var(--f10);font-weight:600;color:rgba(255,255,255,.92);margin-top:1px;white-space:nowrap;letter-spacing:-.2px}
        .day-cost{font-size:var(--f10);font-weight:600;color:white;margin-top:2px;white-space:nowrap;letter-spacing:-.2px}

        /* 南网小程序「智慧用电」日用电量色阶：绝对度数 0/5/10/15/20/25 共 6 档 */
        .day-cell.level-0{background:#7AEB81!important}
        .day-cell.level-1{background:#A6E065!important}
        .day-cell.level-2{background:#F7D04F!important}
        .day-cell.level-3{background:#EA8D3D!important}
        .day-cell.level-4{background:#E46435!important}
        .day-cell.level-5{background:#DE472C!important}
        .day-cell[class*="level-"] .day-num{color:rgba(18,42,18,.72)!important}
        .day-cell[class*="level-"] .day-cost,
        .day-cell[class*="level-"] .day-kwh{color:#17251a!important}

        /* 底部统计 */
        .stats{display:flex;gap:var(--g8);margin-top:var(--g);flex-wrap:wrap;min-width:0}
        .stat-left,.stat-mid,.stat-right{flex:1 1 0;min-width:0;background:rgba(255,255,255,.06);
          border-radius:var(--rad3);padding:var(--f12)}
        .tier-header,.stat-mid-title,.peak-title{font-size:var(--f10);color:rgba(255,255,255,.5);margin-bottom:4px}
        .tier-current{font-size:var(--f13);font-weight:600;color:#69f0ae;margin-bottom:2px}
        .tier-info{font-size:var(--f10);color:rgba(255,255,255,.7);white-space:normal;overflow-wrap:anywhere;line-height:1.35}
        .tier-info span{color:rgba(255,255,255,.5)}
        .stat-mid-row,.peak-row{display:flex;justify-content:space-between;margin-top:4px;min-width:0}
        .stat-mid-item,.peak-item{text-align:center;flex:1 1 0;min-width:0}
        .stat-mid-label,.peak-label{font-size:var(--f9);color:rgba(255,255,255,.4)}
        .stat-mid-value,.peak-value{font-size:var(--f14);font-weight:600;color:white;white-space:nowrap}

        /* 图例 */
        .legend{display:flex;align-items:flex-end;gap:var(--g8);margin-top:var(--f12);
          font-size:var(--f10);color:rgba(255,255,255,.5);min-width:0}
        .legend-main{flex:1 1 0;min-width:0}
        .legend-bar{display:flex;gap:2px;min-width:0}
        .legend-block{flex:1 1 0;min-width:0;height:min(12px,calc(var(--csg-w)*.03));border-radius:2px}
        .legend-ticks{display:flex;justify-content:space-between;margin-top:3px;font-size:var(--f9);color:rgba(255,255,255,.45)}
        .legend-unit{flex:0 0 auto;padding-bottom:14px;white-space:nowrap}

        .footer{padding:var(--f12) var(--p) var(--p2);display:flex;justify-content:space-between;
          align-items:center;gap:var(--g8);flex-wrap:wrap;font-size:var(--f11);
          color:rgba(255,255,255,.4);min-width:0}
        .detail-btn{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.24);
          border-radius:999px;padding:6px 14px;color:#fff;font-size:var(--f11);font-weight:600;
          cursor:pointer;font-family:inherit;white-space:nowrap;transition:background .2s,transform .2s}
        .detail-btn:hover{background:rgba(255,255,255,.28);transform:translateY(-1px)}
        .detail-btn:active{transform:translateY(0)}
      </style>
      
      <div class="card" id="card">
        <!-- 折叠状态：只显示关键指标 -->
        <div class="collapsed-content" id="collapsed-content">
          <div class="collapsed-metrics">
            <div class="collapsed-metric">
              <div class="collapsed-icon">💰</div>
              <div class="collapsed-value" id="col-cost">--</div>
              <div class="collapsed-label">本月电费</div>
            </div>
            <div class="collapsed-metric">
              <div class="collapsed-icon">🔋</div>
              <div class="collapsed-value" id="col-kwh">--</div>
              <div class="collapsed-label">本月用电量</div>
            </div>
            <div class="collapsed-metric">
              <div class="collapsed-icon">📅</div>
              <div class="collapsed-value" id="col-year">--</div>
              <div class="collapsed-label" id="col-year-label">本年累计</div>
            </div>
          </div>
          <div style="font-size:20px;color:rgba(255,255,255,.6)">▼</div>
        </div>
        
        <!-- 展开状态：显示全部内容 -->
        <div class="expanded-content">
          <div class="header">
            <div class="title">
              <div class="title-left">
                <span class="icon">⚡</span>
                <div>
                  <div class="title-text">南方电网</div>
                  <div class="subtitle" id="period-subtitle">本月用电明细</div>
                </div>
              </div>
              <span class="collapse-btn" id="collapse-btn">▲</span>
            </div>
          </div>
          
          <div class="main">
            <div class="metrics">
              <div class="metric">
                <div class="metric-label">
                  <span class="metric-icon">💰</span>
                  <span id="cost-label">本月电费</span>
                </div>
                <div class="metric-value" id="cost">--<span class="metric-unit">元</span></div>
              </div>
              <div class="metric">
                <div class="metric-label">
                  <span class="metric-icon">🔋</span>
                  <span id="kwh-label">本月用电量</span>
                </div>
                <div class="metric-value" id="kwh">--<span class="metric-unit">度</span></div>
              </div>
              <div class="metric">
                <div class="metric-label">
                  <span class="metric-icon">📅</span>
                  <span id="year-label">本年累计</span>
                </div>
                <div class="metric-value" id="year-info">--<span class="metric-unit">度/元</span></div>
              </div>
            </div>
            
            <!-- 翻页按钮 -->
            <div class="page-nav">
              <button class="page-btn" id="prev-month">◀ 上月</button>
              <button class="page-btn" id="current-month" style="flex:1">返回本月</button>
              <button class="page-btn" id="next-month">下月 ▶</button>
            </div>
            
            <div class="compare-row" id="compare">
              <span>📊</span>
              <span>加载中...</span>
            </div>
            
            <div class="calendar-area">
              <div class="calendar-header">
                <span class="calendar-title" id="month-title">本月每日用电</span>
              </div>
              <div class="weekday-row">
                <div class="weekday">一</div>
                <div class="weekday">二</div>
                <div class="weekday">三</div>
                <div class="weekday">四</div>
                <div class="weekday">五</div>
                <div class="weekday">六</div>
                <div class="weekday">日</div>
              </div>
              <div class="calendar-grid" id="calendar"></div>
              <div class="legend">
                <div class="legend-main">
                  <div class="legend-bar">
                    <div class="legend-block" style="background:#7AEB81"></div>
                    <div class="legend-block" style="background:#A6E065"></div>
                    <div class="legend-block" style="background:#F7D04F"></div>
                    <div class="legend-block" style="background:#EA8D3D"></div>
                    <div class="legend-block" style="background:#E46435"></div>
                    <div class="legend-block" style="background:#DE472C"></div>
                  </div>
                  <div class="legend-ticks"><span>0</span><span>5</span><span>10</span><span>15</span><span>20</span><span>25+</span></div>
                </div>
                <span class="legend-unit">度/日</span>
              </div>
            </div>
            
            <!-- 底部统计 - 阶梯移到最左边 -->
            <div class="stats">
              <!-- 左侧：当前阶梯 -->
              <div class="stat-left">
                <div class="tier-header" id="tier-header">当前阶梯</div>
                <div class="tier-current" id="tier-current">--</div>
                <div class="tier-info">
                  <span>总量</span> <b id="tier-total">--</b>度 · <span>剩余</span> <b id="tier-remaining">--</b>度
                </div>
              </div>
              
              <!-- 中间：日均 -->
              <div class="stat-mid">
                <div class="stat-mid-title">日均</div>
                <div class="stat-mid-row">
                  <div class="stat-mid-item">
                    <div class="stat-mid-label">电费</div>
                    <div class="stat-mid-value" id="avg-cost">--</div>
                  </div>
                  <div class="stat-mid-item">
                    <div class="stat-mid-label">用电</div>
                    <div class="stat-mid-value" id="avg-kwh">--</div>
                  </div>
                </div>
              </div>
              
              <!-- 右侧：最高用电 -->
              <div class="stat-right">
                <div class="peak-title">最高用电</div>
                <div class="peak-row">
                  <div class="peak-item">
                    <div class="peak-label">电量</div>
                    <div class="peak-value" id="peak-day">--</div>
                  </div>
                  <div class="peak-item">
                    <div class="peak-label">电费</div>
                    <div class="peak-value" id="peak-cost">--</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          
          <div class="footer">
            <span id="update-time">更新于 --:--</span>
            <button type="button" class="detail-btn" id="detail-btn" title="打开每日电费查询页">📄 详细信息 ↗</button>
          </div>
        </div>
      </div>
    `;
    
    // 点击卡片切换折叠状态
    this.shadowRoot.getElementById('card').addEventListener('click', (e) => {
      // 翻页按钮、详细信息按钮不切换折叠状态
      if (e.target.classList.contains('page-btn') || e.target.classList.contains('detail-btn')) return;
      
      this._expanded = !this._expanded;
      this.shadowRoot.getElementById('card').classList.toggle('expanded', this._expanded);
    });
    
    // 「详细信息」：打开每日电费查询页（同源相对路径，可用 detail_url 覆盖）
    this.shadowRoot.getElementById('detail-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      const url = (this._config && this._config.detail_url) || DEFAULT_DETAIL_URL;
      window.open(url, '_blank');
    });
    
    // 收起按钮事件
    this.shadowRoot.getElementById('collapse-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      this._expanded = false;
      this.shadowRoot.getElementById('card').classList.remove('expanded');
    });
    
    // 翻页按钮事件
    this.shadowRoot.getElementById('prev-month').addEventListener('click', () => {
      this._viewMonthOffset--;
      this._update();
    });
    
    this.shadowRoot.getElementById('next-month').addEventListener('click', () => {
      this._viewMonthOffset++;
      this._update();
    });
    
    this.shadowRoot.getElementById('current-month').addEventListener('click', () => {
      this._viewMonthOffset = 0;
      this._update();
    });

    // v1.5.2：按卡片实测宽度驱动 CSS 缩放变量，7 列日历任何宽度都完整显示
    this._fit = () => {
      const w = this.getBoundingClientRect().width || 420;
      this.style.setProperty('--csg-w', Math.round(w) + 'px');
      this.toggleAttribute('csg-narrow', w < 460);
    };
    this._fit();
    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(() => this._fit());
      this._ro.observe(this);
    } else {
      window.addEventListener('resize', this._fit);
    }
  }

  connectedCallback() {
    if (this._ro) this._ro.observe(this);
    if (this._fit) this._fit();
  }
  disconnectedCallback() { if (this._ro) this._ro.disconnect(); }

  // 按当前查看年份汇总年度累计（电量 + 阶梯估算电费）
  // 数据源：集成实体的 history_by_month（跨年全部账单月份），不依赖具体户号。
  _refreshYearInfo(viewYear) {
    const sr = this.shadowRoot;
    if (!sr) return;
    const y = Number(viewYear);
    const isThisYear = (y === new Date().getFullYear());
    const label = isThisYear ? '本年累计' : y + '年累计';
    const set = (id, text) => { const el = sr.getElementById(id); if (el) el.textContent = text; };
    set('year-label', label);
    set('col-year-label', label);

    const states = (this._hass && this._hass.states) || {};
    let months = null, official = 0;
    const yEnt = this._state('this_year_total_usage');
    if (yEnt) {
      const ya = yEnt.attributes || {};
      months = Array.isArray(ya.history_by_month) ? ya.history_by_month
        : (Array.isArray(ya.this_year_by_month) ? ya.this_year_by_month : null);
      official = parseFloat(yEnt.state) || 0;
    }
    if (!months) {
      for (const id of Object.keys(states)) {
        const a = states[id].attributes || {};
        if (Array.isArray(a.history_by_month) || Array.isArray(a.this_year_by_month)) {
          months = a.history_by_month || a.this_year_by_month;
          official = parseFloat(states[id].state) || 0;
          break;
        }
      }
    }

    const prefix = y + '-';

    const rows = (months || []).filter(m => String(m.month || '').indexOf(prefix) === 0);
    let kwh = 0, cost = 0;
    for (const m of rows) {
      const k = Number(m.kwh) || 0;
      if (k <= 0) continue;
      kwh += k;
      const monthNum = parseInt(String(m.month).split('-')[1], 10) || 1;
      cost += this._calcTierForMonth(k, monthNum);
    }
    if (isThisYear && official > kwh) kwh = official;
    const has = rows.length > 0 || (isThisYear && kwh > 0);
    const yi = this.shadowRoot.getElementById('year-info');
    if (yi) yi.innerHTML = has
      ? '<span class="yi">' + kwh.toFixed(0) + '度</span>' +
        '<span class="yi-sep">/</span>' +
        '<span class="yi">' + cost.toFixed(0) + '元</span>'
      : '<span class="yi">--</span>';
    set('col-year', has ? cost.toFixed(0) + '元' : '--');
  }

  _update() {
    const cfg = this._config || {};
    const states = (this._hass && this._hass.states) || {};
    let mainId = cfg.entity;
    // v1.5.3：卡片配置里的 ID 已失效（实体被改成拼音 / 中文）时自动定位本月用电量实体
    if (!mainId || !states[mainId]) mainId = this._suffixIndex()['this_month_total_usage'] || mainId;
    if (!mainId || !states[mainId]) return;
    const p = this._entityPrefix();

    // 按「集成里的角色」判断月报 / 年报，不再只看 ID 字符串
    const role = this._roleOf(mainId);
    const isYearly = role.indexOf('this_year') === 0 || mainId.includes('this_year');
    const isLastYear = role.indexOf('last_year') === 0 || mainId.includes('last_year');
    
    // 计算要显示的月份（考虑翻页）
    const now = new Date();
    const viewDate = new Date(now.getFullYear(), now.getMonth() + this._viewMonthOffset, 1);
    const viewYear = viewDate.getFullYear();
    const viewMonth = viewDate.getMonth() + 1;
    const monthNames = ['','1','2','3','4','5','6','7','8','9','10','11','12'];

    // 年度累计只看当前翻到的年份（历史月份分支 return 之前先刷新）
    this._refreshYearInfo(viewYear);
    
    // 是否是查看历史月份
    const isViewingPast = this._viewMonthOffset < 0;
    const isViewingFuture = this._viewMonthOffset > 0;
    const isCurrentMonth = this._viewMonthOffset === 0;
    
    // 更新翻页按钮状态
    // 上月按钮：年度视图禁用，查看下月/未来月份时禁用
    this.shadowRoot.getElementById('prev-month').disabled = isYearly || isViewingFuture;
    // 下月按钮：年度视图禁用，查看上月/当前月份/未来月份时禁用
    this.shadowRoot.getElementById('next-month').disabled = isYearly || !isViewingPast;
    // 只有查看历史月份时才显示"返回本月"按钮
    this.shadowRoot.getElementById('current-month').style.display = isViewingPast ? 'block' : 'none';
    
    // 标题
    let periodText = '';
    if (isYearly) {
      periodText = viewYear + '年';
    } else {
      periodText = viewYear + '年' + monthNames[viewMonth] + '月';
    }
    this.shadowRoot.getElementById('period-subtitle').textContent = periodText + '用电明细';
    this.shadowRoot.getElementById('month-title').textContent = periodText + ' 每日用电';
    this.shadowRoot.getElementById('cost-label').textContent = isYearly ? '本年电费' : '本月电费';
    this.shadowRoot.getElementById('kwh-label').textContent = isYearly ? '年用电量' : '本月用电量';
    
    // 获取数据 - 根据翻页调整
    let costEntity, usageEntity, lastPeriodCost;
    
    if (isYearly) {
      costEntity = this._state('this_year_total_cost');
      usageEntity = this._state('this_year_total_usage');
      lastPeriodCost = this._state('last_year_total_cost');
    } else if (isLastYear) {
      costEntity = this._state('last_year_total_cost');
      usageEntity = this._state('last_year_total_usage');
      lastPeriodCost = this._state('this_year_total_cost');
    } else if (isViewingPast) {
      // 历史月份：统一从集成生成的每日电费 JSON 取数。
      // 先清空日历，避免上一月数据残留（v42 的主要故障现象）。
      costEntity = { state: 0 };
      usageEntity = { state: 0, attributes: { this_month_by_day: [] } };
      lastPeriodCost = { state: 0 };
      this._renderCalendar([], false, viewYear, viewMonth);
      this._fetchHistoryData(viewYear, viewMonth, periodText);
      return;
    } else {
      // 当前月份 - 使用本月实体
      costEntity = this._state('this_month_total_cost');
      usageEntity = this._state('this_month_total_usage');
      lastPeriodCost = this._state('last_month_total_cost');
    }
    
    let totalCost = parseFloat(costEntity?.state) || 0;
    let totalKwh = parseFloat(usageEntity?.state) || 0;

    // v1.5.3：电费实体被改名 / 找不到时，用明细里的真实分摊电费求和
    if (totalCost === 0 && usageEntity?.attributes) {
      const ua = usageEntity.attributes;
      const rows = ua.this_month_by_day || ua.this_year_by_month || ua.history_by_month || [];
      const sum = rows.reduce((acc, d) => acc + (Number(d && d.charge) || 0), 0);
      if (sum > 0) totalCost = sum;
    }

    // 如果电费为0，根据用电量计算
    if (totalCost === 0 && totalKwh > 0) {
      const tierInfo = this._calcTier(totalKwh, viewMonth);
      totalCost = tierInfo.cost;
    }
    
    // 更新展开状态
    this.shadowRoot.getElementById('cost').innerHTML = totalCost.toFixed(1) + '<span class="metric-unit">元</span>';
    this.shadowRoot.getElementById('kwh').innerHTML = totalKwh.toFixed(1) + '<span class="metric-unit">度</span>';
    
    // 更新折叠状态
    this.shadowRoot.getElementById('col-cost').textContent = totalCost.toFixed(0) + '元';
    this.shadowRoot.getElementById('col-kwh').textContent = totalKwh.toFixed(0) + '度';
    
    // 年度累计由 _refreshYearInfo(viewYear) 按查看年份统一更新
    
    // 日均对比 - 只在当前月份时显示
    const compareEl = this.shadowRoot.getElementById('compare');
    if (isViewingPast || isViewingFuture || isYearly) {
      // 查看历史/未来月份或年度视图时不显示对比
      compareEl.innerHTML = '<span>📊</span> ' + periodText + ' 累计 ' + totalKwh.toFixed(1) + '度 / ' + totalCost.toFixed(1) + '元';
    } else {
      // 当前月份显示日均对比 - 改为比较电量(度)而不是电费(元)
      const thisMonthDays = now.getDate();
      const lastMonthDays = new Date(now.getFullYear(), now.getMonth(), 0).getDate();
      
      // 获取上月用电量
      const lastMonthEntity = this._state('last_month_total_usage');
      const lastMonthKwh = parseFloat(lastMonthEntity?.state) || 0;
      
      // 日均电量对比
      const thisMonthDailyKwh = thisMonthDays > 0 ? totalKwh / thisMonthDays : 0;
      const lastMonthDailyKwh = lastMonthDays > 0 ? lastMonthKwh / lastMonthDays : 0;
      
      console.log('========== 日均对比调试 ==========');
      console.log('thisMonthDays:', thisMonthDays, 'lastMonthDays:', lastMonthDays);
      console.log('totalKwh(本月):', totalKwh, 'lastMonthKwh(上月):', lastMonthKwh);
      console.log('thisMonthDailyKwh(日均本月):', thisMonthDailyKwh.toFixed(2), '度/天');
      console.log('lastMonthDailyKwh(日均上月):', lastMonthDailyKwh.toFixed(2), '度/天');
      console.log('diff %:', lastMonthDailyKwh > 0 ? ((thisMonthDailyKwh - lastMonthDailyKwh) / lastMonthDailyKwh * 100).toFixed(1) : 'N/A');
      
      if (lastMonthDailyKwh > 0) {
        const diff = ((thisMonthDailyKwh - lastMonthDailyKwh) / lastMonthDailyKwh * 100).toFixed(1);
        if (diff > 0) {
          compareEl.innerHTML = '<span class="compare-tag up">↑ ' + Math.abs(diff) + '%</span> 日均较上月增加';
        } else if (diff < 0) {
          compareEl.innerHTML = '<span class="compare-tag down">↓ ' + Math.abs(diff) + '%</span> 日均较上月下降';
        } else {
          compareEl.innerHTML = '<span>📊</span> 日均与上月持平';
        }
      } else {
        compareEl.innerHTML = '<span>📊</span> 数据计算中...';
      }
    }
    
    // 每日/每月数据
    let dailyData = [];
    if (isYearly) {
      dailyData = usageEntity?.attributes?.this_year_by_month || [];
    } else if (isViewingPast) {
      // 历史月份使用缓存数据或空（等待异步加载）
      const cacheKey = viewYear + '-' + viewMonth.toString().padStart(2, '0');
      dailyData = this._historyDataCache[cacheKey] || [];
    } else {
      // 当前月份 - 使用本月实体
      dailyData = usageEntity?.attributes?.this_month_by_day || [];
    }
    
    if (dailyData.length > 0) {
      // 日均
      const avgCost = totalCost / dailyData.length;
      const avgKwh = totalKwh / dailyData.length;
      this.shadowRoot.getElementById('avg-cost').textContent = avgCost.toFixed(1) + '元';
      this.shadowRoot.getElementById('avg-kwh').textContent = avgKwh.toFixed(1) + '度';
      
      // 峰值/最高用电
      const peakData = dailyData.reduce((max, d) => (d.kwh || 0) > (max.kwh || 0) ? d : max, dailyData[0]);
      const peakKwh = peakData?.kwh || 0;
      // peak-day: 电量, peak-cost: 电费
      this.shadowRoot.getElementById('peak-day').textContent = peakKwh.toFixed(1) + '度';
      this.shadowRoot.getElementById('peak-cost').textContent = (Number.isFinite(Number(peakData.charge)) ? Number(peakData.charge) : peakKwh * 0.66).toFixed(1) + '元';
      
      // 日历标题显示每日用电
      this.shadowRoot.getElementById('month-title').textContent = periodText + ' 每日用电';
      
      // 阶梯（使用查看的月份）
      const tierInfo = this._calcTier(totalKwh, viewMonth);
      this.shadowRoot.getElementById('tier-header').textContent = '当前阶梯 · ' + tierInfo.season;
      this.shadowRoot.getElementById('tier-current').textContent = tierInfo.label;
      this.shadowRoot.getElementById('tier-total').textContent = tierInfo.total;
      this.shadowRoot.getElementById('tier-remaining').textContent = tierInfo.remaining;
      
      // 渲染日历
      this._renderCalendar(dailyData, isYearly, viewYear, viewMonth);
    } else if (isViewingPast) {
      // 历史月份但数据还在加载中，显示加载提示
      this.shadowRoot.getElementById('avg-cost').textContent = '--';
      this.shadowRoot.getElementById('avg-kwh').textContent = '--';
      this.shadowRoot.getElementById('peak-day').textContent = '--';
      this.shadowRoot.getElementById('peak-cost').textContent = '--';
      this.shadowRoot.getElementById('tier-current').textContent = '--';
      this.shadowRoot.getElementById('tier-total').textContent = '--';
      this.shadowRoot.getElementById('tier-remaining').textContent = '--';
      this.shadowRoot.getElementById('tier-header').textContent = '当前阶梯';
      // 日历显示为空
      this._renderCalendar([], false, viewYear, viewMonth);
    }
    
    const now2 = new Date();
    this.shadowRoot.getElementById('update-time').textContent = '更新于 ' + 
      now2.getHours().toString().padStart(2,'0') + ':' + 
      now2.getMinutes().toString().padStart(2,'0');
  }
  
  // 更新历史月份UI（异步数据返回后调用）
  _updateHistoryUI(dayData, periodText, viewYear, viewMonth) {
    console.log('========== _updateHistoryUI 调试 ==========');
    console.log('dayData条数:', dayData?.length);
    console.log('periodText:', periodText);
    console.log('viewYear:', viewYear, 'viewMonth:', viewMonth);
    console.log('viewMonthOffset:', this._viewMonthOffset);
    console.log('dayData前3条:', JSON.stringify(dayData?.slice(0,3)));

    if (!dayData || dayData.length === 0) {
      console.log('dayData为空，不更新UI');
      return;
    }

    const totalKwh = dayData.reduce((sum, d) => sum + (d.kwh || 0), 0);
    const totalCost = dayData.reduce((sum, d) => sum + (d.charge || 0), 0);
    
    // 更新顶部累计
    this.shadowRoot.getElementById('cost').innerHTML = totalCost.toFixed(1) + '<span class="metric-unit">元</span>';
    this.shadowRoot.getElementById('kwh').innerHTML = totalKwh.toFixed(1) + '<span class="metric-unit">度</span>';
    this.shadowRoot.getElementById('col-cost').textContent = totalCost.toFixed(0) + '元';
    this.shadowRoot.getElementById('col-kwh').textContent = totalKwh.toFixed(0) + '度';
    
    // 更新累计显示
    const compareEl = this.shadowRoot.getElementById('compare');
    compareEl.innerHTML = '<span>📊</span> ' + periodText + ' 累计 ' + totalKwh.toFixed(1) + '度 / ' + totalCost.toFixed(1) + '元';
    
    // 更新日均
    const avgCost = totalCost / dayData.length;
    const avgKwh = totalKwh / dayData.length;
    this.shadowRoot.getElementById('avg-cost').textContent = avgCost.toFixed(1) + '元';
    this.shadowRoot.getElementById('avg-kwh').textContent = avgKwh.toFixed(1) + '度';
    
    // 更新峰值
    const peakData = dayData.reduce((max, d) => (d.kwh || 0) > (max.kwh || 0) ? d : max, dayData[0]);
    const peakKwh = peakData?.kwh || 0;
    this.shadowRoot.getElementById('peak-day').textContent = peakKwh.toFixed(1) + '度';
    this.shadowRoot.getElementById('peak-cost').textContent = (Number.isFinite(Number(peakData.charge)) ? Number(peakData.charge) : peakKwh * 0.66).toFixed(1) + '元';
    
    // 更新顶部标题 - 确保两个标题同步
    this.shadowRoot.getElementById('period-subtitle').textContent = periodText + '用电明细';
    
    // 更新阶梯
    const tierInfo = this._calcTier(totalKwh, viewMonth);
    this.shadowRoot.getElementById('tier-header').textContent = '当前阶梯 · ' + tierInfo.season;
    this.shadowRoot.getElementById('tier-current').textContent = tierInfo.label;
    this.shadowRoot.getElementById('tier-total').textContent = tierInfo.total;
    this.shadowRoot.getElementById('tier-remaining').textContent = tierInfo.remaining;
    
    // 渲染日历
    this._renderCalendar(dayData, false, viewYear, viewMonth);
  }
  
  _calcTier(kwh, month) {
    // 深圳市居民阶梯电价（2026年起执行）
    // 夏季(5-10月): 0-260度0.66元，261-600度0.71元，600度以上0.96元
    // 非夏季(1-4月,11-12月): 0-200度0.66元，201-400度0.71元，400度以上0.96元
    const isSummer = month >= 5 && month <= 10;
    
    const tiers = isSummer ? [
      { min: 0, max: 260, label: '第一档', price: 0.66 },
      { min: 260, max: 600, label: '第二档', price: 0.71 },
      { min: 600, max: Infinity, label: '第三档', price: 0.96 }
    ] : [
      { min: 0, max: 200, label: '第一档', price: 0.66 },
      { min: 200, max: 400, label: '第二档', price: 0.71 },
      { min: 400, max: Infinity, label: '第三档', price: 0.96 }
    ];
    
    let currentTier = tiers[2];
    let cost = 0;
    let remaining = 0;
    let tierNum = 3;
    
    if (kwh <= tiers[0].max) {
      cost = kwh * tiers[0].price;
      remaining = tiers[0].max - kwh;
      tierNum = 1;
    } else if (kwh <= tiers[1].max) {
      cost = tiers[0].max * tiers[0].price + (kwh - tiers[0].max) * tiers[1].price;
      remaining = tiers[1].max - kwh;
      tierNum = 2;
    } else {
      cost = tiers[0].max * tiers[0].price + 
             (tiers[1].max - tiers[0].max) * tiers[1].price + 
             (kwh - tiers[1].max) * tiers[2].price;
      remaining = 0;
      tierNum = 3;
    }
    
    currentTier = tiers[tierNum - 1];
    const total = currentTier.max === Infinity ? Math.round(kwh) : currentTier.max;
    return { 
      label: currentTier.label, 
      remaining: Math.round(remaining),
      total: total,
      tierNum: tierNum,
      season: isSummer ? '夏季' : '冬季',
      cost: cost
    };
  }
  
  _calcTierForMonth(kwh, month) {
    // 深圳市居民阶梯电价（2026年起执行）
    const isSummer = month >= 5 && month <= 10;
    
    const tiers = isSummer ? [
      { min: 0, max: 260, price: 0.66 },
      { min: 260, max: 600, price: 0.71 },
      { min: 600, max: Infinity, price: 0.96 }
    ] : [
      { min: 0, max: 200, price: 0.66 },
      { min: 200, max: 400, price: 0.71 },
      { min: 400, max: Infinity, price: 0.96 }
    ];
    
    let cost = 0;
    if (kwh <= tiers[0].max) {
      cost = kwh * tiers[0].price;
    } else if (kwh <= tiers[1].max) {
      cost = tiers[0].max * tiers[0].price + (kwh - tiers[0].max) * tiers[1].price;
    } else {
      cost = tiers[0].max * tiers[0].price + 
             (tiers[1].max - tiers[0].max) * tiers[1].price + 
             (kwh - tiers[1].max) * tiers[2].price;
    }
    
    return cost;
  }

  // 从 China Southern Power Grid Statistics 生成的每日电费 JSON 取历史数据
  _fetchHistoryData(viewYear, viewMonth, periodText) {
    const _self = this;
    const monthStr = viewYear + '-' + viewMonth.toString().padStart(2, '0');
    this._monthKey = monthStr;                 // 防止异步返回覆盖新翻的月份
    this._historyDataCache = this._historyDataCache || {};

    const cached = this._historyDataCache[monthStr];
    if (cached && cached.length > 0) {
      this._updateHistoryUI(cached, periodText, viewYear, viewMonth);
      const note = (this._fallbackNotes || {})[monthStr];
      if (note) {
        const el = this.shadowRoot.getElementById('compare');
        if (el) el.innerHTML = '<span>📊</span> ' + note;
      }
      return;
    }

    const showEmpty = (reason) => {
      if (_self._monthKey !== monthStr) return;
      const sr = _self.shadowRoot;
      const set = (id, val) => { const e = sr.getElementById(id); if (e) e.textContent = val; };
      const el = sr.getElementById('compare');
      if (el) el.innerHTML = '<span>📊</span> ' + periodText + ' 暂无数据（' + reason + '）';
      // 清空所有指标，避免残留上一个月数值造成误读
      ['cost', 'kwh', 'col-cost', 'col-kwh', 'avg-cost', 'avg-kwh',
       'peak-day', 'peak-cost', 'tier-current', 'tier-total', 'tier-remaining']
        .forEach(id => set(id, '--'));
      set('tier-header', '当前阶梯');
      _self._renderCalendar([], false, viewYear, viewMonth);
    };

    // v47: a 0 CNY bill (prepaid credit / not settled) has no daily rows in
    // the JSON until sensor.py v4 is installed. Until then, read the official
    // monthly kWh from the integration entity and spread it over the month,
    // clearly labelled as an estimate. Once v4 writes real rows, the JSON
    // path above wins and this fallback is never used again.
    const billFallback = () => {
      const states = (_self._hass && _self._hass.states) || {};
      let months = null;
      for (const id of Object.keys(states)) {
        const a = states[id].attributes || {};
        if (Array.isArray(a.history_by_month)) {
          months = a.history_by_month;
          break;
        }
      }
      if (!months) return { days: [], zero: false };
      const rec = months.find(r => (r.month || '') === monthStr);
      if (!rec) return { days: [], zero: false };
      const kwh = Number(rec.kwh) || 0;
      const charge = Number(rec.charge) || 0;
      if (kwh <= 0) return { days: [], zero: true };
      const n = new Date(viewYear, viewMonth, 0).getDate();
      const per = kwh / n;
      const perCost = charge > 0 ? charge / n : 0;
      const days = [];
      for (let d = 1; d <= n; d++) {
        days.push({
          date: monthStr + '-' + String(d).padStart(2, '0'),
          kwh: per, charge: perCost, tier: '', tier_price: 0
        });
      }
      console.log('[csg-power-card] ' + monthStr + ' 账单均摊兜底 ' + days.length + ' 天 / ' + kwh + ' 度');
      return { days, zero: false };
    };

    const applyDays = (dayData, emptyReason, fallbackNote) => {
      if (_self._monthKey !== monthStr) return;
      if (!dayData || dayData.length === 0) {
        showEmpty(emptyReason || '南网该月暂无账单');
        return;
      }
      _self._historyDataCache[monthStr] = dayData;
      _self._fallbackNotes = _self._fallbackNotes || {};
      _self._fallbackNotes[monthStr] = fallbackNote || '';
      _self._updateHistoryUI(dayData, periodText, viewYear, viewMonth);
      if (fallbackNote) {
        const el = _self.shadowRoot.getElementById('compare');
        if (el) el.innerHTML = '<span>📊</span> ' + fallbackNote;
      }
    };

    fetch('/local/csg_daily_cost_history.json?_=' + Date.now())
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(j => {
        const accounts = j.accounts || {};
        const accKey = (_self._config && _self._config.account) || Object.keys(accounts)[0];
        const blk = accounts[accKey] || accounts[Object.keys(accounts)[0]] || {};
        const days = blk.days || {};
        const allDates = Object.keys(days).sort();
        const earliest = allDates.length ? allDates[0].slice(0, 7) : null;
        const prefix = monthStr + '-';
        let dayData = Object.keys(days)
          .filter(d => d.indexOf(prefix) === 0)
          .sort()
          .map(d => {
            const row = days[d];
            return {
              date: d,
              kwh: Number(row[0]) || 0,
              charge: Number(row[1]) || 0,
              tier: row[3],
              tier_price: row[4]
            };
          });
        console.log('[csg-power-card] ' + monthStr + ' 从 JSON 取得 ' + dayData.length + ' 天');
        let emptyReason = null, fallbackNote = null;
        if (dayData.length === 0) {
          const fb = billFallback();
          dayData = fb.days;
          if (fb.zero) {
            emptyReason = '南网该月账单为 0 元且未返回用电量（预缴抵扣 / 未出账）';
          } else if (dayData.length > 0) {
            fallbackNote = periodText + ' 账单电费 0 元（预缴抵扣 / 未出账）；' +
              '用电量按账单均摊估算，安装 v4 传感器补丁后显示真实每日电量';
          }
        }
        if (dayData.length === 0 && earliest && monthStr < earliest) {
          showEmpty('历史数据从 ' + earliest + ' 起');
          return;
        }
        applyDays(dayData, emptyReason, fallbackNote);
      })
      .catch(e => {
        console.log('[csg-power-card] JSON 读取失败:', e.message);
        showEmpty('数据文件暂不可用');
      });
  }

  _renderCalendar(dailyData, isYearly, viewYear, viewMonth) {
    const calendarEl = this.shadowRoot.getElementById('calendar');
    
    // 调试日志
    console.log('========== _renderCalendar 调试 ==========');
    console.log('dailyData:', dailyData?.length, '条');
    console.log('isYearly:', isYearly, 'viewYear:', viewYear, 'viewMonth:', viewMonth);
    console.log('this._viewMonthOffset:', this._viewMonthOffset);
    
    const now = new Date();
    let year, month, today;
    
    if (isYearly) {
      year = viewYear;
      month = viewMonth - 1; // 年视图：1-12
      today = 31; // 年视图显示全年
    } else {
      year = viewYear;
      month = viewMonth - 1; // 转换为0-11
      
      // 获取该月天数 - 正确方式
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      
      if (this._viewMonthOffset === 0) {
        // 本月显示到今天
        today = now.getDate();
      } else if (this._viewMonthOffset < 0) {
        // 历史月份：显示该月所有天数
        today = daysInMonth;
        console.log('历史月份，显示', today, '天');
      } else {
        today = 0; // 下月不显示
      }
    }
    
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const firstDay = new Date(year, month, 1).getDay();
    const adjustedFirstDay = firstDay === 0 ? 6 : firstDay - 1;
    
    const dataMap = {};
    console.log('========== 日历渲染数据调试 ==========');
    console.log('dailyData条数:', dailyData?.length);
    console.log('dailyData前5条:', JSON.stringify(dailyData?.slice(0,5)));
    
    // 详细处理每个日期
    if (dailyData && dailyData.length > 0) {
      dailyData.forEach((d, idx) => {
        let key;
        if (isYearly) {
          key = parseInt(d.month?.split('-')[1]);
        } else {
          // 处理日期格式
          const dateStr = d.date;
          console.log('处理第', idx+1, '条: date=', dateStr);
          if (dateStr && dateStr.includes('-')) {
            const dateParts = dateStr.split('-');
            // 可能是 "2025-02-01" 或 "2025-2-1" 格式
            const dayPart = dateParts[2]?.replace(/^0/, '') || dateParts[2]; // 去掉前导0
            key = parseInt(dayPart, 10);
            console.log('  分割结果:', dateParts, '-> key:', key);
          } else {
            console.log('  日期格式异常');
            key = null;
          }
        }
        if (key !== null && !isNaN(key)) {
          dataMap[key] = d;
        }
      });
    }
    
    console.log('dataMap keys (排序):', Object.keys(dataMap).map(k => parseInt(k)).sort((a,b)=>a-b));
    console.log('daysInMonth:', daysInMonth);
    console.log('数据匹配天数:', Object.keys(dataMap).length);
    
    const kwhValues = dailyData.map(d => d.kwh);
    const minKwh = kwhValues.length > 0 ? Math.min(...kwhValues) : 0;
    const maxKwh = kwhValues.length > 0 ? Math.max(...kwhValues) : 0;
    const range = maxKwh - minKwh || 1;
    
    let html = '';
    
    if (isYearly) {
      // 年视图
      for (let m = 1; m <= 12; m++) {
        if (m <= (month + 1) && dataMap[m]) {
          const d = dataMap[m];
          const level = Math.min(5, Math.floor((d.kwh - minKwh) / range * 5));
          html += `<div class="day-cell level-${level}">
            <span class="day-num">${m}月</span>
            <span class="day-cost">${(Number(d.charge !== undefined ? d.charge : d.cost) || 0).toFixed(1)}元</span>
            <span class="day-kwh">${(Number(d.kwh) || 0).toFixed(1)}度</span>
          </div>`;
        } else {
          html += `<div class="day-cell empty"></div>`;
        }
      }
    } else {
      // 月视图
      // 空白
      for (let i = 0; i < adjustedFirstDay; i++) {
        html += `<div class="day-cell empty"></div>`;
      }
      // 日期 - 方块内显示电量
      for (let d = 1; d <= daysInMonth; d++) {
        if (d > today && this._viewMonthOffset === 0) {
          html += `<div class="day-cell future">
            <span class="day-num">${d}</span>
          </div>`;
        } else if (dataMap[d]) {
          const dayData = dataMap[d];
          const level = this._csgLevel(dayData.kwh);
          html += `<div class="day-cell level-${level}">
            <span class="day-num">${d}日</span>
            <span class="day-cost">${(Number(dayData.charge) || 0).toFixed(1)}元</span>
            <span class="day-kwh">${(Number(dayData.kwh) || 0).toFixed(1)}度</span>
          </div>`;
        } else {
          // 找不到数据的日期留空
          html += `<div class="day-cell">
            <span class="day-num">${d}日</span>
          </div>`;
        }
      }
    }
    
    calendarEl.innerHTML = html;
  }
};
if (!customElements.get('csg-power-card')) {
  customElements.define('csg-power-card', CSGPowerCard);
}
for (const alias of ['custom-power-card-v47', 'custom-power-card-v48']) {
  if (!customElements.get(alias)) {
    customElements.define(alias, class extends CSGPowerCard {});
  }
}
