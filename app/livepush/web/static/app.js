/* livepush 前端：原生 JS，无构建链。 */

const LP = {
  async api(path, opts = {}) {
    const { method = 'GET', body = null, silent = false } = opts;
    const init = { method, headers: { 'X-LP': '1' }, credentials: 'same-origin' };
    if (body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('未登录');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) {
      const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
      if (!silent) LP.toast(msg, 'err');
      throw new Error(msg);
    }
    return data;
  },

  toast(msg, kind = '') {
    const wrap = document.getElementById('toasts');
    if (!wrap) return;
    const node = document.createElement('div');
    node.className = `toast ${kind}`;
    node.textContent = String(msg);
    wrap.appendChild(node);
    setTimeout(() => node.remove(), kind === 'err' ? 6000 : 3500);
  },

  esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  },

  el(tag, attrs = {}, html = '') {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    if (html) node.innerHTML = html;
    return node;
  },

  empty(text) { return `<div class="empty">${LP.esc(text)}</div>`; },

  badge(text, cls = '') { return `<span class="badge ${cls}">${LP.esc(text)}</span>`; },

  modal(title, contentNode, actions) {
    const backdrop = LP.el('div', { class: 'modal-backdrop' });
    const modal = LP.el('div', { class: 'modal' });
    modal.appendChild(LP.el('h3', {}, LP.esc(title)));
    const body = LP.el('div');
    body.appendChild(contentNode);
    modal.appendChild(body);
    const bar = LP.el('div', { class: 'actions' });
    (actions || [{ label: '关闭', cls: '', onClick: null }]).forEach((action) => {
      const btn = LP.el('button', { class: action.cls || '' }, LP.esc(action.label));
      btn.addEventListener('click', async () => {
        try {
          const keep = action.onClick ? await action.onClick() : false;
          if (!keep) backdrop.remove();
        } catch (e) { /* 已 toast */ }
      });
      bar.appendChild(btn);
    });
    modal.appendChild(bar);
    backdrop.appendChild(modal);
    backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) backdrop.remove(); });
    document.body.appendChild(backdrop);
    return { backdrop, modal, close: () => backdrop.remove() };
  },
};

const pages = {};

/* ------------------------------------------------------------------ 登录 */
pages.login = () => {
  const form = document.getElementById('login-form');
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const password = document.getElementById('password').value;
    try {
      await LP.api('/api/login', { method: 'POST', body: { password } });
      window.location.href = '/';
    } catch (e) { /* 已提示 */ }
  });
};

/* ------------------------------------------------------------------ 总览 */
pages.dashboard = async () => {
  const cards = document.getElementById('cards');
  const botsBox = document.getElementById('bots');
  const eventsBox = document.getElementById('events');
  const riskPanel = document.getElementById('risk-panel');
  const riskText = document.getElementById('risk-text');

  const render = (d) => {
    const c = d.counts;
    const engine = d.engine || {};
    const items = [
      ['在播', c.live],
      ['直播间总数', c.rooms],
      ['推送目标', c.targets],
      ['今日已发送', c.sent_today],
      ['今日失败', c.failed_today],
      ['待发送', c.pending],
    ];
    cards.innerHTML = items.map(([k, v]) => `
      <div class="card"><div class="k">${LP.esc(k)}</div><div class="v">${LP.esc(v)}</div></div>`).join('')
      + `<div class="card"><div class="k">监控</div><div class="v" style="font-size:15px">${engine.running ? '运行中' : '已停止'}</div>
         <div class="small muted">${engine.last_tick_at ? '上次检查 ' + new Date(engine.last_tick_at * 1000).toLocaleTimeString() : '尚未检查'}</div></div>`;

    if (engine.risk && engine.risk.blocked) {
      riskPanel.hidden = false;
      riskText.textContent = `B 站风控冷却中，剩余 ${engine.risk.remaining} 秒。原因：${engine.risk.reason}。期间不会误判下播。`;
    } else if (engine.last_error) {
      riskPanel.hidden = false;
      riskText.textContent = `最近一次采集失败：${engine.last_error}`;
    } else {
      riskPanel.hidden = true;
    }

    if (!d.bots.length) {
      botsBox.innerHTML = LP.empty('还没有 QQ 连接，去「QQ 账号与登录」添加。');
    } else {
      botsBox.innerHTML = `<table><thead><tr><th>名称</th><th>协议</th><th>状态</th><th>账号</th></tr></thead><tbody>${
        d.bots.map((b) => `<tr><td>${LP.esc(b.name)}</td><td>${LP.esc(b.transport)}</td>
          <td>${LP.badge(b.state_label, b.state === 'logged_in' || b.state === 'connected' ? 'ok' : (b.state === 'error' ? 'bad' : 'warn'))}</td>
          <td class="mono">${LP.esc(b.self_id || b.nickname || '-')}</td></tr>`).join('')
      }</tbody></table>`;
    }

    eventsBox.innerHTML = d.events.length ? d.events.map((e) => `
      <li><span class="muted small">${LP.esc(e.at_text)}</span>
        ${LP.badge(e.label, e.kind === 'live_on' ? 'live' : (e.kind === 'live_off' ? 'dim' : 'ok'))}
        <strong>${LP.esc(e.uname)}</strong>
        <span class="muted">${LP.esc(e.title || '')}</span>
        ${e.duration ? `<span class="small muted">（${LP.esc(e.duration)}）</span>` : ''}
      </li>`).join('') : LP.empty('还没有事件。添加直播间后静待开播。');
  };

  const load = async () => {
    try { render(await LP.api('/api/overview')); } catch (e) { /* 忽略轮询错误 */ }
  };
  await load();
  setInterval(load, 5000);

  // ---------------------------------------------------------------- 实时日志
  const logBox = document.getElementById('live-log');
  const levelSel = document.getElementById('log-level');
  const areaSel = document.getElementById('log-area');
  const MAX_LINES = 300;
  let logLines = [];
  let logCursor = 0;
  let logPaused = false;
  let logStream = null;

  const levelColor = (level) =>
    (level === 'ERROR' || level === 'CRITICAL') ? '#ff6b6b' : (level === 'WARNING' ? '#f0b429' : '#8b95a7');

  const renderLog = () => {
    if (!logBox) return;
    const area = areaSel.value;
    const shown = logLines.filter((l) => !area || l.area === area).slice(-MAX_LINES);
    logBox.textContent = '';
    for (const line of shown) {
      const el = document.createElement('div');
      el.style.color = levelColor(line.level);
      el.textContent = `${line.time} ${String(line.level).padEnd(7)} ${line.area} ${line.message}`;
      logBox.appendChild(el);
    }
    logBox.scrollTop = logBox.scrollHeight;
  };

  const appendLog = (items) => {
    if (logPaused || !items || !items.length) return;
    for (const item of items) {
      logLines.push(item);
      if (item.seq > logCursor) logCursor = item.seq;
    }
    if (logLines.length > 1200) logLines = logLines.slice(-800);
    renderLog();
  };

  const connectLogStream = () => {
    if (logStream) logStream.close();
    const level = encodeURIComponent(levelSel.value);
    logStream = new EventSource(`/api/logs/stream?cursor=${logCursor}&level=${level}`);
    logStream.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'log') appendLog(data.lines);
      } catch (e) { /* 忽略单条解析失败 */ }
    });
  };

  try {
    const first = await LP.api('/api/logs?limit=300');
    logLines = first.lines || [];
    logCursor = first.seq || 0;
    (first.areas || []).forEach((area) => {
      const opt = document.createElement('option');
      opt.value = area;
      opt.textContent = area;
      areaSel.appendChild(opt);
    });
    renderLog();
    connectLogStream();
  } catch (e) { /* 日志面板不是关键路径 */ }

  levelSel?.addEventListener('change', async () => {
    try {
      const d = await LP.api(`/api/logs?limit=300&level=${encodeURIComponent(levelSel.value)}`);
      logLines = d.lines || [];
      logCursor = d.seq || 0;
      renderLog();
      connectLogStream();
    } catch (e) { /* 忽略 */ }
  });
  areaSel?.addEventListener('change', renderLog);
  document.getElementById('btn-log-pause')?.addEventListener('click', (ev) => {
    logPaused = !logPaused;
    ev.target.textContent = logPaused ? '继续' : '暂停';
    if (!logPaused) connectLogStream();
  });
  document.getElementById('btn-log-clear')?.addEventListener('click', () => {
    logLines = [];
    renderLog();
  });

  document.getElementById('btn-check').addEventListener('click', async () => {
    await LP.api('/api/check', { method: 'POST' });
    LP.toast('已安排立即检查', 'ok');
    setTimeout(load, 1200);
  });

  try {
    const es = new EventSource('/api/stream');
    es.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'live_event') load();
      } catch (e) { /* 忽略 */ }
    });
  } catch (e) { /* 浏览器不支持就靠轮询 */ }
};

/* ------------------------------------------------------------------ 直播间 */
pages.rooms = async () => {
  const box = document.getElementById('rooms');
  const preview = document.getElementById('preview');
  let pending = null;

  const load = async () => {
    const d = await LP.api('/api/rooms');
    if (!d.rooms.length) { box.innerHTML = LP.empty('还没有关注任何直播间。'); return; }
    box.innerHTML = `<table><thead><tr>
      <th>主播</th><th>状态</th><th>标题</th><th>最后检查</th><th>绑定</th><th class="right">操作</th>
    </tr></thead><tbody>${d.rooms.map((r) => `
      <tr>
        <td><img class="avatar" src="${LP.esc(r.face || '/static/favicon.svg')}" alt=""> ${LP.esc(r.uname || r.uid)}
          <div class="small muted mono">uid ${r.uid}${r.room_id ? ' · 房间 ' + r.room_id : ''}</div></td>
        <td>${r.live ? LP.badge('直播中', 'live') : LP.badge('未开播', 'dim')}
            ${r.room_state !== 'ok' ? LP.badge(r.room_state_label, r.room_state === 'stale' ? 'warn' : 'warn') : ''}
            ${r.enabled ? '' : LP.badge('已停用', 'bad')}</td>
        <td class="truncate" title="${LP.esc(r.title)}">${LP.esc(r.title || '-')}</td>
        <td class="small muted">${LP.esc(r.last_checked_text)}</td>
        <td>${r.bindings}</td>
        <td class="right nowrap">
          <button class="small" data-act="events" data-id="${r.id}">事件</button>
          <button class="small" data-act="toggle" data-id="${r.id}" data-enabled="${r.enabled ? 1 : 0}">${r.enabled ? '停用' : '启用'}</button>
          <button class="small danger" data-act="delete" data-id="${r.id}">删除</button>
        </td>
      </tr>`).join('')}</tbody></table>`;
  };

  box.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === 'delete') {
      if (!window.confirm('删除这个直播间？相关绑定也会一起删除。')) return;
      await LP.api(`/api/rooms/${id}`, { method: 'DELETE' });
      LP.toast('已删除', 'ok');
      load();
    } else if (btn.dataset.act === 'toggle') {
      await LP.api(`/api/rooms/${id}`, { method: 'PATCH', body: { enabled: btn.dataset.enabled !== '1' } });
      load();
    } else if (btn.dataset.act === 'events') {
      const d = await LP.api(`/api/rooms/${id}/events`);
      const node = LP.el('div', {}, d.events.length ? `<table><thead><tr><th>时间</th><th>事件</th><th>时长</th></tr></thead><tbody>${
        d.events.map((e) => `<tr><td class="small muted">${LP.esc(e.at_text)}</td><td>${LP.esc(e.label)}</td><td class="small">${LP.esc(e.duration || '-')}</td></tr>`).join('')
      }</tbody></table>` : LP.empty('还没有事件'));
      LP.modal('事件历史', node);
    }
  });

  document.getElementById('btn-refresh').addEventListener('click', load);

  document.getElementById('btn-resolve').addEventListener('click', async () => {
    const query = document.getElementById('add-query').value.trim();
    if (!query) return;
    try {
      const d = await LP.api('/api/rooms/resolve', { method: 'POST', body: { query } });
      const r = d.room;
      pending = r;
      preview.hidden = false;
      preview.innerHTML = `
        <div class="card wide" style="display:flex;gap:14px;align-items:center">
          <img class="avatar" style="width:44px;height:44px" src="${LP.esc(r.face || '/static/favicon.svg')}" alt="">
          <div style="flex:1">
            <div><strong>${LP.esc(r.uname)}</strong> ${r.live ? LP.badge('正在直播', 'live') : LP.badge('未开播', 'dim')}</div>
            <div class="small muted">uid ${r.uid} · 房间 ${r.room_id || r.short_id || '-'} · ${LP.esc(r.area || '')}</div>
            <div class="small muted">${LP.esc(r.title || '')}</div>
          </div>
          <button class="primary" id="btn-confirm-add">添加这个直播间</button>
        </div>`;
      document.getElementById('btn-confirm-add').addEventListener('click', async () => {
        if (!pending) return;
        await LP.api('/api/rooms', {
          method: 'POST',
          body: { query: String(pending.uid), notify_on_add: document.getElementById('add-notify').checked },
        });
        LP.toast('已添加', 'ok');
        preview.hidden = true;
        pending = null;
        load();
      });
    } catch (e) { /* 已提示 */ }
  });

  await load();
};

/* ------------------------------------------------------------------ 推送目标 */
pages.targets = async () => {
  const box = document.getElementById('targets');
  const botSelect = document.getElementById('t-bot');
  let bots = [];

  const loadBots = async () => {
    const d = await LP.api('/api/bots');
    bots = d.bots;
    botSelect.innerHTML = bots.length
      ? bots.map((b) => `<option value="${b.id}">${LP.esc(b.name)}（${LP.esc(b.transport)}）</option>`).join('')
      : '<option value="">先创建 QQ 连接</option>';
  };

  const load = async () => {
    const d = await LP.api('/api/targets');
    if (!d.targets.length) { box.innerHTML = LP.empty('还没有推送目标。'); return; }
    box.innerHTML = `<table><thead><tr><th>名称</th><th>类型</th><th>ID</th><th>机器人</th><th>状态</th><th class="right">操作</th></tr></thead><tbody>${
      d.targets.map((t) => `<tr>
        <td>${LP.esc(t.name)}</td>
        <td>${t.kind === 'group' ? LP.badge('群聊') : LP.badge('私聊')}</td>
        <td class="mono">${LP.esc(t.target_id)}</td>
        <td>${LP.esc(t.bot_name)}</td>
        <td>${t.enabled ? LP.badge('启用', 'ok') : LP.badge('停用', 'bad')}</td>
        <td class="right nowrap">
          <button class="small" data-act="test" data-id="${t.id}">测试</button>
          <button class="small" data-act="toggle" data-id="${t.id}" data-enabled="${t.enabled ? 1 : 0}">${t.enabled ? '停用' : '启用'}</button>
          <button class="small danger" data-act="delete" data-id="${t.id}">删除</button>
        </td></tr>`).join('')}</tbody></table>`;
  };

  box.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    try {
      if (btn.dataset.act === 'delete') {
        if (!window.confirm('删除这个推送目标？相关绑定会一起删除。')) return;
        await LP.api(`/api/targets/${id}`, { method: 'DELETE' });
        load();
      } else if (btn.dataset.act === 'toggle') {
        await LP.api(`/api/targets/${id}`, { method: 'PATCH', body: { enabled: btn.dataset.enabled !== '1' } });
        load();
      } else if (btn.dataset.act === 'test') {
        btn.disabled = true;
        const d = await LP.api('/api/test-push', { method: 'POST', body: { target_id: Number(id) } });
        LP.toast(d.ok ? '测试消息已发出' : '发送失败', d.ok ? 'ok' : 'err');
        btn.disabled = false;
      }
    } catch (e) { /* 已提示 */ }
  });

  document.getElementById('btn-add-target').addEventListener('click', async () => {
    const botId = Number(botSelect.value);
    if (!botId) { LP.toast('先创建一个 QQ 连接', 'warn'); return; }
    const targetId = document.getElementById('t-id').value.trim();
    if (!targetId) return;
    await LP.api('/api/targets', {
      method: 'POST',
      body: {
        bot_connection_id: botId,
        kind: document.getElementById('t-kind').value,
        target_id: targetId,
        name: document.getElementById('t-name').value.trim(),
      },
    });
    document.getElementById('t-id').value = '';
    document.getElementById('t-name').value = '';
    LP.toast('已添加', 'ok');
    load();
  });

  document.getElementById('btn-import').addEventListener('click', async () => {
    const botId = Number(botSelect.value);
    if (!botId) { LP.toast('先创建一个 QQ 连接', 'warn'); return; }
    const d = await LP.api('/api/targets/import-groups', { method: 'POST', body: { bot_connection_id: botId } });
    LP.toast(d.message, 'ok');
    load();
  });

  await loadBots();
  await load();
};

/* ------------------------------------------------------------------ 绑定矩阵 */
pages.bindings = async () => {
  const box = document.getElementById('matrix');
  let data = null;

  const key = (roomId, targetId) => `${roomId}:${targetId}`;

  const load = async () => {
    data = await LP.api('/api/bindings');
    if (!data.rooms.length) { box.innerHTML = LP.empty('先添加直播间。'); return; }
    if (!data.targets.length) { box.innerHTML = LP.empty('先添加推送目标。'); return; }
    const map = new Map(data.bindings.map((b) => [key(b.room_id, b.target_id), b]));

    const head = data.targets.map((t) =>
      `<th class="rot" title="${LP.esc(t.name)}（${t.kind}:${t.target_id}）">${LP.esc(t.name || t.target_id)}</th>`).join('');

    const rows = data.rooms.map((r) => {
      const cells = data.targets.map((t) => {
        const b = map.get(key(r.id, t.id));
        const on = b && b.enabled;
        const partial = b && (!b.notify_live_on || !b.notify_live_off) ? ' title="部分通知已关闭"' : '';
        return `<td><button class="cell-btn ${on ? 'on' : ''}" data-room="${r.id}" data-target="${t.id}"${partial}>${on ? '●' : '○'}</button></td>`;
      }).join('');
      return `<tr><td class="room-cell">${r.live ? '🔴' : '⚪'} ${LP.esc(r.uname || r.id)}</td>${cells}</tr>`;
    }).join('');

    box.innerHTML = `<table class="matrix"><thead><tr><th>直播间</th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  };

  box.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button.cell-btn');
    if (!btn) return;
    const roomId = Number(btn.dataset.room);
    const targetId = Number(btn.dataset.target);
    const existing = data.bindings.find((b) => b.room_id === roomId && b.target_id === targetId) || {
      room_id: roomId, target_id: targetId, notify_live_on: true, notify_live_off: true,
      notify_title_change: false, at_all: false, include_cover: true, message_template: '', enabled: true,
    };
    const room = data.rooms.find((r) => r.id === roomId);
    const target = data.targets.find((t) => t.id === targetId);

    const form = LP.el('div', {}, `
      <p class="hint">${LP.esc(room ? room.uname : '')} → ${LP.esc(target ? (target.name || target.target_id) : '')}</p>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-en" style="width:auto" ${existing.enabled ? 'checked' : ''}><span style="margin:0">启用这条绑定</span></label>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-on" style="width:auto" ${existing.notify_live_on ? 'checked' : ''}><span style="margin:0">开播通知</span></label>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-off" style="width:auto" ${existing.notify_live_off ? 'checked' : ''}><span style="margin:0">下播通知</span></label>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-title" style="width:auto" ${existing.notify_title_change ? 'checked' : ''}><span style="margin:0">标题变更通知</span></label>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-at" style="width:auto" ${existing.at_all ? 'checked' : ''}><span style="margin:0">开播时 @全体成员</span></label>
      <label class="field" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="e-cover" style="width:auto" ${existing.include_cover ? 'checked' : ''}><span style="margin:0">带封面图</span></label>
      <label class="field"><span>自定义开播文案（留空用全局模板）</span><textarea id="e-tpl">${LP.esc(existing.message_template || '')}</textarea></label>${existing.id ? `
      <label class="field"><span>测试推送的类型</span>
        <select id="e-test-kind">
          <option value="live_on">开播消息</option>
          <option value="live_off">下播消息</option>
        </select>
      </label>
      <p class="hint">测试消息按真实格式渲染并标注「测试」，不会写进事件/推送记录。</p>` : ''}`);

    const save = async () => {
      await LP.api('/api/bindings', {
        method: 'POST',
        body: {
          room_id: roomId,
          target_id: targetId,
          enabled: document.getElementById('e-en').checked,
          notify_live_on: document.getElementById('e-on').checked,
          notify_live_off: document.getElementById('e-off').checked,
          notify_title_change: document.getElementById('e-title').checked,
          at_all: document.getElementById('e-at').checked,
          include_cover: document.getElementById('e-cover').checked,
          message_template: document.getElementById('e-tpl').value,
        },
      });
      LP.toast('已保存', 'ok');
      load();
      return false;
    };

    const actions = [{ label: '取消' }, { label: '保存', cls: 'primary', onClick: save }];
    if (existing.id) {
      actions.unshift({
        label: '发测试推送', onClick: async () => {
          const kind = document.getElementById('e-test-kind')?.value || 'live_on';
          try {
            const r = await LP.api(`/api/subscriptions/${existing.id}/test-push`, {
              method: 'POST',
              body: { kind },
            });
            LP.toast(r.ok ? '测试消息已发往该目标' : `发送失败：${r.error || '未知错误'}`, r.ok ? 'ok' : 'err');
          } catch (e) { /* LP.api 已提示 */ }
          return false;
        },
      });
    }
    if (existing.id) {
      actions.push({
        label: '解除绑定', cls: 'danger', onClick: async () => {
          await LP.api(`/api/bindings/${existing.id}`, { method: 'DELETE' });
          LP.toast('已解除', 'ok');
          load();
          return false;
        },
      });
    }
    LP.modal('绑定设置', form, actions);
  });

  document.getElementById('btn-refresh').addEventListener('click', load);
  await load();
};

/* ------------------------------------------------------------------ 日志 */
pages.logs = async () => {
  const body = document.getElementById('log-body');
  const filter = document.getElementById('log-filter');

  const load = async () => {
    if (filter.value === 'events') {
      const d = await LP.api('/api/events?limit=200');
      body.innerHTML = d.events.length ? `<table><thead><tr><th>时间</th><th>事件</th><th>主播</th><th>标题</th><th>时长</th></tr></thead><tbody>${
        d.events.map((e) => `<tr><td class="small muted">${LP.esc(e.at_text)}</td>
          <td>${LP.badge(e.label, e.kind === 'live_on' ? 'live' : 'dim')}</td>
          <td>${LP.esc(e.uname)}</td><td class="truncate">${LP.esc(e.title || '-')}</td>
          <td class="small">${LP.esc(e.duration || '-')}</td></tr>`).join('')}</tbody></table>` : LP.empty('还没有事件');
      return;
    }
    const d = await LP.api('/api/jobs?limit=200');
    body.innerHTML = d.jobs.length ? `<table><thead><tr><th>时间</th><th>状态</th><th>事件</th><th>主播</th><th>目标</th><th>尝试</th><th>错误</th><th></th></tr></thead><tbody>${
      d.jobs.map((j) => `<tr>
        <td class="small muted">${LP.esc(j.at_text)}</td>
        <td>${LP.badge(j.status_label, j.status === 'sent' ? 'ok' : (j.status === 'failed' ? 'bad' : 'warn'))}</td>
        <td>${LP.esc(j.event)}</td><td>${LP.esc(j.uname)}</td>
        <td class="small">${LP.esc(j.target)}</td><td>${j.attempts}</td>
        <td class="small truncate" title="${LP.esc(j.error || '')}">${LP.esc(j.error || '-')}</td>
        <td>${j.status === 'failed' ? `<button class="small" data-retry="${j.id}">重试</button>` : ''}</td>
      </tr>`).join('')}</tbody></table>` : LP.empty('还没有推送任务');
  };

  body.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-retry]');
    if (!btn) return;
    await LP.api(`/api/jobs/${btn.dataset.retry}/retry`, { method: 'POST' });
    LP.toast('已重新排队', 'ok');
    load();
  });

  filter.addEventListener('change', load);
  document.getElementById('btn-refresh').addEventListener('click', load);
  await load();
};

/* ------------------------------------------------------------------ 设置 */
pages.settings = async () => {
  const inputs = Array.from(document.querySelectorAll('[data-cfg]'));

  const load = async () => {
    const d = await LP.api('/api/settings');
    for (const input of inputs) {
      const key = input.dataset.cfg;
      const value = d.config[key];
      if (input.type === 'checkbox') input.checked = Boolean(value);
      else input.value = value ?? '';
    }
    const bots = await LP.api('/api/bots');
    if (bots.bots.length) {
      const bot = bots.bots[0];
      const sign = bot.sign || {};
      document.getElementById('sign-url').value = sign.base_url || '';
      document.getElementById('sign-token').value = sign.token || '';
      document.getElementById('sign-version').value = sign.protocol_version || 'fetched';
    }
  };

  document.getElementById('btn-save-settings').addEventListener('click', async () => {
    const config = {};
    for (const input of inputs) {
      const key = input.dataset.cfg;
      if (input.type === 'checkbox') config[key] = input.checked;
      else if (input.type === 'number') config[key] = Number(input.value);
      else config[key] = input.value;
    }
    await LP.api('/api/settings', { method: 'PUT', body: { config } });
    LP.toast('设置已保存', 'ok');
  });

  document.getElementById('btn-sign-save').addEventListener('click', async () => {
    const bots = await LP.api('/api/bots');
    const botId = bots.bots.length ? bots.bots[0].id : null;
    await LP.api('/api/sign', {
      method: 'PUT',
      body: {
        bot_connection_id: botId,
        kind: 'selfhosted',
        base_url: document.getElementById('sign-url').value.trim(),
        token: document.getElementById('sign-token').value.trim(),
        proxy_url: document.getElementById('sign-proxy').value.trim(),
        protocol_version: document.getElementById('sign-version').value.trim() || 'fetched',
      },
    });
    LP.toast('签名配置已保存', 'ok');
  });

  document.getElementById('btn-sign-check').addEventListener('click', async () => {
    const box = document.getElementById('sign-result');
    box.innerHTML = '<span class="muted">检测中…</span>';
    const d = await LP.api('/api/sign/check', {
      method: 'POST',
      body: {
        base_url: document.getElementById('sign-url').value.trim(),
        token: document.getElementById('sign-token').value.trim(),
        proxy_url: document.getElementById('sign-proxy').value.trim(),
        protocol_version: document.getElementById('sign-version').value.trim(),
      },
    });
    const r = d.result;
    box.innerHTML = `<div class="card"><strong>${r.ok ? '✅ 可用' : '❌ 不可用'}</strong>
      <div class="small">${LP.esc(r.message)}</div>
      <div class="small muted">health=${LP.esc(r.health)} platform=${LP.esc(r.platform || '-')} version=${LP.esc(r.version || '-')}</div></div>`;
  });

  const signAction = async (path, label) => {
    try {
      const d = await LP.api(path, { method: 'POST' });
      LP.toast(d.message || label, d.ok === false ? 'err' : 'ok');
      const st = await LP.api('/api/sign/server');
      const logBox = document.getElementById('sign-log');
      if (st.status && st.status.log) {
        logBox.hidden = false;
        document.getElementById('sign-log-text').textContent = st.status.log;
      }
    } catch (e) { /* 已提示 */ }
  };

  document.getElementById('btn-sign-start').addEventListener('click', () => signAction('/api/sign/server/start', '已启动'));
  document.getElementById('btn-sign-stop').addEventListener('click', () => signAction('/api/sign/server/stop', '已停止'));

  const renderLogs = async () => {
    const box = document.getElementById('log-files');
    if (!box) return;
    try {
      const d = await LP.api('/api/logs/files');
      if (!d.files.length) {
        box.innerHTML = '<span class="muted">还没有日志文件</span>';
        return;
      }
      box.innerHTML = `<div class="small muted">目录：${LP.esc(d.dir)}</div>` + d.files.map((f) =>
        `<div class="small">${LP.esc(f.name)} · ${(f.size / 1024).toFixed(1)} KB · ${Number(f.age_days).toFixed(1)} 天 · ${LP.esc(f.area)}${f.protected ? ' · 活动文件' : ''}</div>`
      ).join('');
    } catch (e) {
      box.innerHTML = '<span class="muted">读取失败</span>';
    }
  };

  document.getElementById('btn-refresh-logs')?.addEventListener('click', renderLogs);
  document.getElementById('btn-cleanup-logs')?.addEventListener('click', async () => {
    try {
      const d = await LP.api('/api/logs/cleanup', { method: 'POST' });
      const r = d.report;
      LP.toast(`删除 ${r.deleted.length} 个，释放 ${(r.freed_bytes / 1024).toFixed(1)} KB`, 'ok');
      await renderLogs();
    } catch (e) { /* 已提示 */ }
  });

  const alertStateText = (s) => {
    document.getElementById('alert-state').textContent =
      `已发送 ${s.sent} · 被抑制 ${s.suppressed} · 队列 ${s.queued}${s.last_error ? ' · 最近错误：' + s.last_error : ''}`;
  };
  const loadAlertState = async () => {
    try {
      const d = await LP.api('/api/alerts/state');
      alertStateText(d.state);
    } catch (e) { /* 忽略 */ }
  };
  document.getElementById('btn-alert-test')?.addEventListener('click', async () => {
    const d = await LP.api('/api/alerts/test', { method: 'POST' });
    LP.toast(d.ok ? '测试告警已发出' : (d.error || '发送失败'), d.ok ? 'ok' : 'err');
    await loadAlertState();
  });

  document.getElementById('btn-doctor').addEventListener('click', async () => {
    const panel = document.getElementById('doctor-panel');
    const box = document.getElementById('doctor-result');
    panel.hidden = false;
    box.innerHTML = '<span class="muted">检查中…</span>';
    const d = await LP.api('/api/doctor');
    box.innerHTML = `<table><tbody>${d.checks.map((c) => `<tr>
      <td style="width:28px">${c.ok ? '✅' : '❌'}</td>
      <td style="width:180px">${LP.esc(c.name)}</td>
      <td class="small">${LP.esc(c.detail)}${c.hint ? `<div class="muted">↳ ${LP.esc(c.hint)}</div>` : ''}</td>
    </tr>`).join('')}</tbody></table>`;
  });

  await load();
  await renderLogs();
  await loadAlertState();
};

/* ------------------------------------------------------------------ QQ 连接 */
pages.qq = async () => {
  const box = document.getElementById('bots');
  const modeSel = document.getElementById('b-mode');
  const transportSel = document.getElementById('b-transport');

  const syncForm = () => {
    const mode = modeSel.value;
    const builtin = mode === 'builtin';
    const hosted = mode === 'hosted';
    document.getElementById('wrap-uin').hidden = !(builtin || hosted);
    document.getElementById('wrap-backend').hidden = !builtin;
    document.getElementById('wrap-hosted-backend').hidden = !hosted;
    document.getElementById('wrap-api').hidden = builtin || hosted || transportSel.value !== 'milky';
    document.getElementById('wrap-ws').hidden = builtin || hosted || transportSel.value !== 'onebot11';
    transportSel.disabled = hosted;
    if (builtin) transportSel.value = 'milky';
    if (hosted) transportSel.value = 'onebot11';
  };
  modeSel.addEventListener('change', syncForm);
  transportSel.addEventListener('change', syncForm);
  syncForm();

  const load = async () => {
    const d = await LP.api('/api/bots');
    if (!d.bots.length) { box.innerHTML = LP.empty('还没有 QQ 连接。上面创建一个。'); return; }
    box.innerHTML = `<table><thead><tr><th>名称</th><th>方式</th><th>状态</th><th>账号</th><th class="right">操作</th></tr></thead><tbody>${
      d.bots.map((b) => `<tr>
        <td>${LP.esc(b.name)}<div class="small muted">${LP.esc(b.backend_label || b.backend)}</div></td>
        <td>${b.mode === 'builtin' ? LP.badge('内置协议端') : b.mode === 'hosted' ? LP.badge('托管容器') : LP.badge('外接')} ${LP.badge(b.transport)}</td>
        <td>${LP.badge(b.state_label, ['logged_in', 'connected'].includes(b.state) ? 'ok' : (b.state === 'error' ? 'bad' : 'warn'))}
            ${b.last_error ? `<div class="small muted truncate" title="${LP.esc(b.last_error)}">${LP.esc(b.last_error)}</div>` : ''}</td>
        <td class="mono">${LP.esc(b.self_id || b.nickname || '-')}</td>
        <td class="right nowrap">
          ${b.mode === 'builtin' ? `<button class="small primary" data-act="login" data-id="${b.id}">${['logged_in', 'connected'].includes(b.state) ? '查看/重新登录' : '启动登录'}</button>` : ''}
          ${b.mode === 'hosted' ? `<button class="small primary" data-act="hosted" data-id="${b.id}">托管</button>` : ''}
          <button class="small" data-act="test" data-id="${b.id}">检查连接</button>
          <button class="small danger" data-act="delete" data-id="${b.id}">删除</button>
        </td></tr>`).join('')}</tbody></table>`;
  };

  const pollLogin = (botId, holder) => {
    let timer = null;
    const tick = async () => {
      try {
        const d = await LP.api(`/api/bots/${botId}/login`);
        const login = d.login;
        if (!holder.isConnected) return;
        holder.render(login);
        if (login.state === 'logged_in') {
          LP.toast('登录成功', 'ok');
          load();
          clearInterval(timer);
        }
      } catch (e) { /* 忽略 */ }
    };
    tick();
    timer = setInterval(tick, 1500);
    return () => clearInterval(timer);
  };

  const openLogin = async (botId) => {
    const node = LP.el('div', {}, '<div class="muted">正在启动协议端并获取二维码…</div>');
    const holder = {
      isConnected: true,
      render(login) {
        const stateText = {
          stopped: '已停止', preparing: '准备中', launching: '启动中', generating_qr: '生成二维码',
          qr_ready: '等待扫码', logged_in: '已连接', expired: '二维码过期，正在换新', error: '异常',
          reusing_session: '复用上次登录态，正在验证…',
        }[login.state] || login.state;
        let qrHtml = '<div class="muted small">等待二维码…</div>';
        if (login.state === 'logged_in') {
          qrHtml = `<div class="badge ok">已连接</div>`;
        } else if (login.qr && login.qr.data_uri) {
          qrHtml = `<div class="qr-box"><img src="${login.qr.data_uri}" alt="二维码">
            <div class="small muted">用手机 QQ 扫码；剩余 ${login.qr_expires_in || 0} 秒</div></div>`;
        } else if (login.qr && login.qr.text) {
          qrHtml = `<div class="qr-box"><pre>${LP.esc(login.qr.text)}</pre>
            <div class="small muted">终端二维码（可在新窗口放大后扫）</div></div>`;
        }
        node.innerHTML = `
          <div class="steps"><span class="step ${login.state === 'logged_in' ? 'active' : ''}">${LP.esc(stateText)}</span></div>
          ${qrHtml}
          ${login.error ? `<div class="card"><div class="small">${LP.esc(login.error)}</div></div>` : ''}
          <div class="row" style="margin-top:10px">
            <button class="fixed small" id="btn-login-log">查看协议端日志</button>
          </div>
          <pre class="log" id="login-log" hidden></pre>`;
        const logBtn = node.querySelector('#btn-login-log');
        logBtn.addEventListener('click', async () => {
          const d = await LP.api(`/api/bots/${botId}/login/log?lines=100`);
          const pre = node.querySelector('#login-log');
          pre.hidden = false;
          pre.textContent = d.log || '(暂无输出)';
        });
      },
    };

    const { close } = LP.modal('扫码登录', node, [
      { label: '关闭' },
      {
        label: '重新生成二维码', cls: 'small', onClick: async () => {
          await LP.api(`/api/bots/${botId}/login/restart`, { method: 'POST' });
          LP.toast('已重新启动协议端', 'ok');
          return true;
        },
      },
    ]);

    try {
      await LP.api(`/api/bots/${botId}/login/start`, { method: 'POST' });
    } catch (e) {
      holder.render({ state: 'error', error: e.message, qr: null });
    }
    const stopPolling = pollLogin(botId, holder);
    const observer = new MutationObserver(() => {
      if (!document.body.contains(node)) { holder.isConnected = false; stopPolling(); observer.disconnect(); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    const origClose = close;
    return origClose;
  };

  const hostedStateText = (s) => ({
    running: '运行中', exited: '已停止（容器还在）', stopped: '已停止',
    missing: '容器不存在', 'docker-missing': 'Docker 不可用', pending: '待安装',
  }[s] || s || '未知');

  const openHosted = async (botId) => {
    const node = LP.el('div', {}, '<div class="muted">正在读取托管状态…</div>');
    let timer = null;
    let closed = false;
    const stopTimer = () => { if (timer) { clearInterval(timer); timer = null; } };
    const on = (id, fn) => { const el = node.querySelector('#' + id); if (el) el.addEventListener('click', fn); };
    const post = async (path, body = {}) => {
      try {
        await LP.api(path, { method: 'POST', body });
        await render();
        await load();
        return true;
      } catch (e) { return false; }
    };

    const render = async () => {
      let d;
      try { d = await LP.api(`/api/hosted/${botId}/status`); }
      catch (e) { node.innerHTML = `<div class="badge bad">${LP.esc(e.message)}</div>`; return; }
      if (closed) return;
      const s = d.status || {};
      const job = s.job;
      if (!s.installed) {
        node.innerHTML = `<div class="muted">这条连接还没有托管容器。安装会下载镜像（几百 MB）并启动容器。</div>
          <div class="row" style="margin-top:10px"><button class="primary fixed" id="btn-hosted-install">安装并启动</button></div>
          <p class="hint">需要服务器上已装 Docker 并对当前用户可用。下载与启动进度会显示在下面。</p>
          <div id="hosted-progress"></div>`;
      } else {
        const pct = job && job.total ? Math.round((job.downloaded / job.total) * 100) : 0;
        node.innerHTML = `
          <div class="grid2">
            <div><div class="small muted">状态</div><div>${LP.badge(hostedStateText(s.state), s.state === 'running' ? 'ok' : 'warn')}</div></div>
            <div><div class="small muted">容器</div><div class="mono small">${LP.esc(s.container || '-')}</div></div>
            <div><div class="small muted">镜像</div><div class="mono small">${LP.esc(s.image_ref || '-')}</div></div>
            <div><div class="small muted">工作目录</div><div class="mono small">${LP.esc(s.install_dir || '-')}</div></div>
          </div>
          ${s.webui_url ? `<p>WebUI：<a href="${s.webui_url}" target="_blank" rel="noreferrer">${LP.esc(s.webui_url)}</a>　·　<a href="/hosted/${botId}" target="_blank" rel="noreferrer">在 livepush 内打开</a></p>` : ''}
          ${s.remote_url ? `<p>远程桌面：<a href="${s.remote_url}" target="_blank" rel="noreferrer">${LP.esc(s.remote_url)}</a></p>` : ''}
          <p class="small muted">OneBot WS：<span class="mono">${LP.esc(s.ws_url || '-')}</span>　Token：<span class="mono">${LP.esc(s.token || '-')}</span></p>
          <div id="hosted-progress">${job && !job.done ? `<div class="card"><div class="small">${LP.esc(job.message)} ${job.total ? pct + '%' : ''}</div></div>` : ''}</div>
          ${job && job.done && !job.ok ? `<div class="card"><div class="badge bad">安装失败</div><div class="small">${LP.esc(job.error)}</div></div>` : ''}
          ${s.last_error ? `<div class="card"><div class="small">${LP.esc(s.last_error)}</div></div>` : ''}
          ${(s.notes || []).map((n) => `<p class="hint">${LP.esc(n)}</p>`).join('')}
          <div class="row" style="margin-top:10px">
            <button class="small primary" id="btn-hosted-install">安装 / 重装</button>
            <button class="small" id="btn-hosted-start">启动</button>
            <button class="small" id="btn-hosted-attach">连接</button>
            <button class="small" id="btn-hosted-stop">停止</button>
            <button class="small danger" id="btn-hosted-remove">删除容器</button>
            <button class="small" id="btn-hosted-logs">查看日志</button>
          </div>
          <pre class="log" id="hosted-log" hidden>${LP.esc(s.log_tail || '')}</pre>`;
      }
      on('btn-hosted-install', () => post(`/api/hosted/install`, { bot_id: botId }));
      on('btn-hosted-start', () => post(`/api/hosted/${botId}/start`));
      on('btn-hosted-attach', () => post(`/api/hosted/${botId}/attach`));
      on('btn-hosted-stop', () => post(`/api/hosted/${botId}/stop`));
      on('btn-hosted-remove', async () => {
        if (!window.confirm('删除容器？镜像会保留，下次安装可复用。')) return;
        await post(`/api/hosted/${botId}/remove`);
      });
      on('btn-hosted-logs', () => {
        const pre = node.querySelector('#hosted-log');
        if (pre) pre.hidden = !pre.hidden;
      });
      if (job && !job.done) { if (!timer) timer = setInterval(render, 2000); }
      else stopTimer();
    };

    const modal = LP.modal('托管协议端', node, [{ label: '关闭' }]);
    const observer = new MutationObserver(() => {
      if (!document.body.contains(node)) { closed = true; stopTimer(); observer.disconnect(); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    await render();
    return modal.close;
  };

  box.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === 'login') {
      await openLogin(Number(id));
    } else if (btn.dataset.act === 'hosted') {
      await openHosted(Number(id));
    } else if (btn.dataset.act === 'delete') {
      if (!window.confirm('删除这个连接？相关推送目标也会一起删除。')) return;
      await LP.api(`/api/bots/${id}`, { method: 'DELETE' });
      load();
    } else if (btn.dataset.act === 'test') {
      const d = await LP.api(`/api/bots/${id}/login`);
      LP.toast(`状态：${d.login.state}${d.login.error ? '｜' + d.login.error : ''}`, 'warn');
    }
  });

  document.getElementById('btn-add-bot').addEventListener('click', async () => {
    const mode = modeSel.value;
    const builtin = mode === 'builtin';
    const hosted = mode === 'hosted';
    const transport = hosted ? 'onebot11' : transportSel.value;
    const body = {
      name: document.getElementById('b-name').value.trim(),
      transport,
      mode,
      backend: builtin
        ? document.getElementById('b-backend').value
        : hosted
          ? document.getElementById('b-hosted-backend').value
          : (transport === 'onebot11' ? 'onebot11' : 'milky'),
      uin: Number(document.getElementById('b-uin').value || 0),
      api_url: document.getElementById('b-api').value.trim(),
      ws_url: document.getElementById('b-ws').value.trim(),
      token: document.getElementById('b-token').value.trim(),
    };
    try {
      const d = await LP.api('/api/bots', { method: 'POST', body });
      LP.toast('已创建', 'ok');
      await load();
      if (builtin) await openLogin(d.id);
      if (hosted) await openHosted(d.id);
    } catch (e) { /* 已提示 */ }
  });

  document.getElementById('btn-refresh').addEventListener('click', load);
  await load();
};

/* ------------------------------------------------------------------ 启动 */
document.addEventListener('DOMContentLoaded', () => {
  const page = document.body.dataset.page;
  if (page && pages[page]) {
    Promise.resolve(pages[page]()).catch((err) => {
      console.error(err);
      LP.toast(err.message || String(err), 'err');
    });
  }
});
