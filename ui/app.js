/* 吃饱饱助手 · 前端（原生 JS，无框架）
   原则：前端不做任何业务判断 —— 概率、候选、四条推荐、收尾文案全部来自后端真实计算。
   前端只负责：把当下状态画好看、把点击变成一次 API 调用。 */

const API = {
  state: () => fetch('/api/state').then(r => r.json()),
  post: (path, body) => fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(r => r.json()),
};

const COLORS = ['#E8622C', '#F5A623', '#7FA05B', '#C0574E', '#8E6BBF', '#3F8FA6'];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let STATE = null;
let BUSY = false;
let WHEEL_CTX = null;      // 当前转盘的画布与选项（演示时要在同一块画布上真转）
let DEMO_STOP = false;     // 演示中途可点按钮停止

const $ = s => document.querySelector(s);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ============ 启动 ============ */
init();
async function init() {
  bindUI();
  await refresh();
  addSys('我是吃饱饱，今天想好吃啥了不？没想法就把选择权交给命运 🎰');
  addWelcome();
  // 调试/排练钩子：?auto=demo 自动跑一键演示，?auto=wheel 直接起盘，
  // ?auto=card 一路跳到「名片 + 四条」这一屏（答辩排练/排查用，不做动画）
  const auto = new URLSearchParams(location.search).get('auto');
  if (auto === 'demo') runDemo();
  else if (auto === 'wheel') startWheel();
  else if (auto === 'card') jumpToCard();
}

/* 排练/排查用：不做动画，直接把会话推进到「名片 + 四条」那一屏 */
async function jumpToCard() {
  await API.post('/api/reset');
  await API.post('/api/start');
  for (const k of ['正餐', '食堂', '辣']) await API.post('/api/accept', { key: k });
  const r = await API.post('/api/spin');
  const picked = (r.wheel || {}).picked;
  if (picked) await API.post('/api/accept', { key: picked });
  addSys(`已直接跳到「${picked || ''}」的名片 + 四条对比（排练用，未播放动画）`);
  await refresh();
}

function addWelcome() {  const box = el('div', 'welcome');
  box.innerHTML = `
    <div class="w-title">它能干什么</div>
    <ul>
      <li><b>四层转盘</b>：餐别 → 场景 → 口味 → 菜品，概率是真的，拒绝后真的重排</li>
      <li><b>点名直达</b>：说「想吃火锅」就不转盘，直接给四条并排方案</li>
      <li><b>四条并列</b>：综合最佳 / 最近 / 最便宜 / 评分最高，同一个不重复上屏</li>
      <li><b>记得你</b>：偏好写进画像，下次按你的口味和预算预过滤</li>
    </ul>
    <div class="w-foot">模型调用与工具调用都是真的 —— 底部那行日志就是每一次 <code>tool_calls</code>。</div>`;
  $('#chatLog').appendChild(box);
}

function bindUI() {
  $('#btnSend').onclick = send;
  $('#input').onkeydown = e => { if (e.key === 'Enter') send(); };
  document.querySelectorAll('.chip-btn').forEach(b => b.onclick = () => {
    $('#input').value = b.dataset.text;
    send();
  });
  $('#btnDemo').onclick = runDemo;
  $('#btnReset').onclick = async () => {
    await API.post('/api/reset');
    $('#chatLog').innerHTML = '';
    $('#runlog').innerHTML = '';
    addSys('重新开始。今天想好吃啥了不？');
    await refresh();
  };
  $('#btnPref').onclick = () => {
    const p = $('#prefPanel');
    p.hidden = !p.hidden;
  };
  $('#prefClose').onclick = () => { $('#prefPanel').hidden = true; };
}

async function refresh() {
  STATE = await API.state();
  renderMeta();
  renderStage();
  renderPref();
}

/* ============ 顶栏 / 偏好 / 日志 ============ */
function renderMeta() {
  $('#modelBadge').textContent = STATE.model || '未知模型';
  const fb = $('#fallbackTag');
  fb.hidden = !STATE.usedFallback;
  if (STATE.usedFallback) fb.title = STATE.fallbackReason || '';
  const nn = $('#netNote');
  const notes = STATE.notes || [];
  nn.hidden = notes.length === 0;
  nn.textContent = notes.length ? notes[notes.length - 1] : '';
  const logs = (STATE.logs || []).slice(-3);
  $('#runlog').innerHTML = logs.length
    ? logs.map(l => `<div>${esc(l)}</div>`).join('')
    : '<div class="empty">运行日志（工具调用 / 概率 / 落盘 都会打在这里）</div>';
}

function renderPref() {
  const box = $('#prefChips');
  box.innerHTML = '';
  const chips = STATE.chips || [];
  if (!chips.length) box.appendChild(el('span', 'empty', '还没有偏好记录，聊两句我就会记住。'));
  chips.forEach(c => {
    const n = el('span', 'pref-chip', `${esc(c.text)}<button title="删除">×</button>`);
    n.querySelector('button').onclick = async () => {
      await API.post('/api/pref/delete', { field: c.field, value: c.value });
      await refresh();
    };
    box.appendChild(n);
  });
  const r = STATE.recent || {};
  const txt = r.count
    ? `近 ${r.count} 次：常在${r.where || '—'}吃，偏好${r.spice || '—'}，最常点${r.favorite || '—'}，平均 ¥${r.avg_price || '—'}。`
    : '还没有选择记录。';
  $('#prefRecent').textContent = txt;
}

/* ============ 舞台 ============ */
function renderStage() {
  const stage = $('#stage');
  stage.innerHTML = '';
  WHEEL_CTX = null;                 // 画面重建后旧的画布句柄作废，避免演示转到已销毁的 canvas
  const st = STATE || {};
  stage.appendChild(crumbs(st));

  if (st.step === 'done' && st.final) return renderFinal(stage, st);
  if (st.final && st.step === 'done') return renderFinal(stage, st);

  const w = st.wheel;
  if (w && w.scene === 'dish') return renderGrid(stage, st);
  if (w && ['meal', 'where', 'spice'].includes(w.scene)) return renderWheel(stage, st);
  if (st.step === 'card' && st.card) return renderCard(stage, st);
  if (st.four && (st.four.groups || []).length) return renderFourStage(stage, st);

  // 空舞台
  const hero = el('div', 'hero');
  hero.innerHTML = `
    <div class="big">🍱</div>
    <h2>今天这顿，交给它决定</h2>
    <p>四层转盘：餐别 → 场景 → 口味 → 菜品。概率是真的，拒绝之后真的会重排。</p>
    <div class="row">
      <button class="btn primary" id="go">开始转盘</button>
      <button class="btn ghost" id="demo2">▶ 一键演示整场</button>
    </div>`;
  stage.appendChild(hero);
  hero.querySelector('#go').onclick = startWheel;
  hero.querySelector('#demo2').onclick = runDemo;
}

function crumbs(st) {
  const box = el('div', 'crumbs');
  // 名片/收尾阶段转盘已经销毁，选择记录要从 state.choices 取，否则面包屑会整排变灰
  const c = st.choices || (st.wheel && st.wheel.choices) || {};
  const items = [
    ['餐别', st.mealLabel || c.meal],
    ['场景', c.where],
    ['口味', c.spice === '随便' ? '随便（不过滤）' : c.spice],
    ['菜品', c.dish],
  ];
  items.forEach(([k, v]) => {
    const on = !!v;
    box.appendChild(el('span', 'crumb' + (on ? ' on' : ''), `${k}${v ? '：' + esc(v) : ''}`));
  });
  if (st.budget && st.budget !== '不限') box.appendChild(el('span', 'crumb on', `预算 ≤ ¥${esc(st.budget)}`));
  return box;
}

/* ---------- 转盘 ---------- */
function renderWheel(stage, st) {
  const w = st.wheel;
  if (!w) return renderStage();
  stage.appendChild(el('h2', 'stage-title', esc(w.title)));
  if (w.ctx) stage.appendChild(el('p', 'stage-sub', '上一层：' + esc(w.ctx)));

  const wrap = el('div', 'wheel-wrap');
  const box = el('div', 'wheel-box');
  const cv = el('canvas'); cv.id = 'wheelCanvas';
  const center = el('button', 'wheel-center', '转一下');
  center.id = 'spinBtn';
  box.appendChild(cv); box.appendChild(center);
  wrap.appendChild(box);

  // 概率常驻显示（变化时 200ms 过渡由 CSS 负责）
  const legend = el('div', 'prob-legend');
  w.options.forEach((o, i) => {
    const dead = o.prob <= 0;
    const p = el('span', 'prob' + (dead ? ' dead' : ''),
      `<span class="dot" style="background:${COLORS[i % COLORS.length]}"></span>${esc(o.key)} <b>${Math.round(o.prob * 100)}%</b>`);
    legend.appendChild(p);
  });
  // 被拒绝过的项：红灰色 + 删除线，永久剔出本会话（权重清零）
  (w.rejected || []).forEach(k => {
    legend.appendChild(el('span', 'prob dead',
      `<span class="dot" style="background:#B9AFA6"></span>${esc(k)} <b>已拒</b>`));
  });
  wrap.appendChild(legend);

  // 预算（仅菜品层可调）与操作
  const actions = el('div', 'row center');
  const accept = el('button', 'btn primary', '就要这个');
  const reject = el('button', 'btn ghost', '换一个（拒绝）');
  accept.disabled = true; reject.disabled = true;
  actions.appendChild(accept); actions.appendChild(reject);

  // 「随便」不占扇区：它和真实选项混在一张盘上，拒掉一个之后就没有悬念了，
  // 所以放在盘外当按钮 —— 点了就是跳过本层，交给后端 50/50 掷一次。
  if (w.skip) {
    const sk = el('button', 'btn ghost skip', esc(w.skip.label || '都行，你替我定'));
    sk.onclick = async () => {
      if (BUSY) return;
      BUSY = true;
      await API.post('/api/accept', { key: w.skip.key });
      BUSY = false;
      addSys('行，这层我替你定了。');
      await refresh();
    };
    actions.appendChild(sk);
  }

  if (w.scene === 'dish') {
    const sel = el('select', 'picker');
    (w.budgetOptions || []).forEach(v => {
      const o = el('option', null, v === '不限' ? '金额上限：不限' : `金额上限：¥${v}`);
      o.value = v;
      if (String(v) === String(w.budget)) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = async () => {
      await API.post('/api/budget', { value: sel.value });
      await refresh();
    };
    wrap.appendChild(sel);
  }

  stage.appendChild(wrap);
  stage.appendChild(actions);

  drawWheel(cv, w.options, 0, -1);
  WHEEL_CTX = { canvas: cv, options: w.options, scene: w.scene };

  let picked = null;
  // 只剩一个选项：概率 100%，转也白转 —— 直接锁定，让人点「就要这个」
  if (w.options.length === 1) {
    picked = w.options[0].key;
    accept.disabled = false;
    reject.disabled = true;          // 只剩这一个，再拒就空了，干脆不让拒
    center.disabled = true;
    center.textContent = '只剩一个';
    stage.insertBefore(el('p', 'stage-sub', `这层只剩「${picked}」了，没得转，直接定就行。`), wrap);
  }
  center.onclick = async () => {
    if (BUSY) return;
    BUSY = true;
    center.disabled = true;
    const res = await API.post('/api/spin');
    const wv = res.wheel;
    if (!wv) { center.disabled = false; BUSY = false; return; }
    const n = wv.options.length;
    const target = -(wv.pickedIndex + 0.5) * (Math.PI * 2 / n) + Math.PI * 8;
    await spinAnim(cv, wv.options, target, 3200);
    drawWheel(cv, wv.options, target, wv.pickedIndex);
    syncProbs(wv);
    picked = wv.picked;
    accept.disabled = false; reject.disabled = false;
    center.disabled = false; BUSY = false;
    addSys(`转到「${wv.picked}」，接受还是换一个？`);
  };

  accept.onclick = async () => {
    if (!picked) return;
    const r = await API.post('/api/accept', { key: picked });
    if (r.note) addSys(r.note);
    if (r.accepted) addSys(`✅ ${picked}`);
    if (r.card) await landOnCard(r);
    await refresh();
  };

  reject.onclick = async () => {
    if (!picked) return;
    const r = await API.post('/api/reject', { key: picked });
    if (r.note) addSys(r.note);
    await refresh();
  };
}

function syncProbs(wv) {
  const scale = (wv.scene === 'dish' && wv.items) ? wv.items.length : wv.options.length;
  document.querySelectorAll('.prob').forEach((p, i) => {
    const o = wv.options[i];
    if (!o) return;
    p.querySelector('b').textContent = Math.round(o.prob * 100) + '%';
    p.classList.toggle('dead', o.prob <= 0);
    p.classList.toggle('hit', i === wv.pickedIndex);
  });
}

function drawWheel(canvas, opts, rot, sel) {
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 360;
  canvas.width = size * dpr; canvas.height = size * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);
  const cx = size / 2, cy = size / 2, R = size / 2 - 6;

  // 外环（细分刻度）
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = '#fff'; ctx.fill();
  ctx.strokeStyle = '#EADDCB'; ctx.lineWidth = 2; ctx.stroke();
  for (let i = 0; i < 72; i++) {
    const a = (i / 72) * Math.PI * 2 + rot;
    const r0 = R - 2, r1 = R - (i % 6 === 0 ? 10 : 5);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
    ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    ctx.strokeStyle = i % 6 === 0 ? '#E2C9A8' : '#F1E6D6';
    ctx.lineWidth = 1; ctx.stroke();
  }

  const n = opts.length || 1;
  const seg = Math.PI * 2 / n;
  opts.forEach((o, i) => {
    const a0 = i * seg - Math.PI / 2 + rot;
    const a1 = a0 + seg;
    const isSel = i === sel;
    const rr = (R - 12) * (isSel ? 1.04 : 1);
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, rr, a0, a1); ctx.closePath();
    ctx.fillStyle = COLORS[i % COLORS.length];
    ctx.globalAlpha = o.prob > 0 ? 1 : .35;
    if (isSel) { ctx.shadowColor = 'rgba(232,98,44,.5)'; ctx.shadowBlur = 20; }
    ctx.fill();
    ctx.shadowBlur = 0; ctx.globalAlpha = 1;
    ctx.strokeStyle = '#fff'; ctx.lineWidth = isSel ? 3 : 2; ctx.stroke();

    const mid = a0 + seg / 2;
    ctx.save();
    ctx.translate(cx + Math.cos(mid) * rr * .60, cy + Math.sin(mid) * rr * .60);
    // 文字沿半径方向排布；左半圈再翻 180°，避免出现倒立的字
    ctx.rotate(mid + (Math.cos(mid) < 0 ? Math.PI : 0));
    ctx.textAlign = 'center';
    ctx.fillStyle = '#fff';
    ctx.font = '700 15px "PingFang SC","Microsoft YaHei",sans-serif';
    ctx.fillText(o.key, 0, -7);
    ctx.font = '600 12px ui-monospace,Consolas,monospace';
    ctx.fillText(Math.round(o.prob * 100) + '%', 0, 12);
    ctx.restore();
  });

  // 中心底盘（HTML 按钮盖在上面）
  ctx.beginPath(); ctx.arc(cx, cy, size * .11, 0, Math.PI * 2);
  ctx.fillStyle = '#fff'; ctx.fill();
  ctx.strokeStyle = '#F3E3CE'; ctx.lineWidth = 2; ctx.stroke();
}

function spinAnim(canvas, opts, target, dur) {
  return new Promise(resolve => {
    const t0 = performance.now();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      drawWheel(canvas, opts, target, -1);
      resolve();
    };
    const step = t => {
      if (done) return;
      const p = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      drawWheel(canvas, opts, target * e, -1);
      if (p < 1) requestAnimationFrame(step); else finish();
    };
    requestAnimationFrame(step);
    // 兜底：窗口最小化 / 后台标签页 / 无头渲染时 rAF 可能不再产生新帧，
    // 只靠 rAF 会把整场演示卡死在半途，这里用定时器保证一定能收尾。
    setTimeout(finish, dur + 400);
  });
}

/* ---------- 菜品格子 ---------- */
function renderGrid(stage, st) {
  const w = st.wheel;
  const items = w.items || [];
  if (!items.length) {
    stage.appendChild(el('p', 'empty', '这一层没筛出菜，换个条件试试。'));
    return;
  }
  stage.appendChild(el('h2', 'stage-title', esc(w.title)));
  const sub = w.note
    ? `${esc(w.note)}　｜　每格都是真实数据（价格 / 距离来自后端）`
    : `共 ${items.length} 道菜 · 每格都是真实数据（价格 / 距离来自后端，不是前端假数据）`;
  stage.appendChild(el('p', 'stage-sub', sub));

  // 预算
  const sel = el('select', 'picker');
  (w.budgetOptions || []).forEach(v => {
    const o = el('option', null, v === '不限' ? '金额上限：不限' : `金额上限：¥${v}`);
    o.value = v;
    if (String(v) === String(w.budget)) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = async () => {
    await API.post('/api/budget', { value: sel.value });
    await refresh();
  };
  const bar = el('div', 'row');
  const spin = el('button', 'btn primary', '转一下');
  spin.id = 'gridSpin';
  bar.appendChild(spin); bar.appendChild(sel);
  stage.appendChild(bar);

  const grid = el('div', 'grid');
  items.forEach((it, i) => {
    const c = el('div', 'cell',
      `<img src="${esc(it.img || '/plate/' + it.id + '.svg')}" alt="${esc(it.name)}" loading="lazy">
       <div class="cell-meta"><span class="cell-name">${esc(it.name)}</span>
       <span class="cell-price">¥${it.price}</span></div>`);
    c.dataset.i = i;
    grid.appendChild(c);
  });
  stage.appendChild(grid);

  const actions = el('div', 'row');
  const accept = el('button', 'btn primary', '就要这个');
  const reject = el('button', 'btn ghost', '换一个（拒绝）');
  accept.disabled = reject.disabled = true;
  actions.appendChild(accept); actions.appendChild(reject);
  stage.appendChild(actions);

  let picked = null;
  spin.onclick = async () => {
    if (BUSY) return;
    BUSY = true; spin.disabled = true;
    const res = await API.post('/api/spin');
    const wv = res.wheel;
    if (!wv) { BUSY = false; spin.disabled = false; return; }
    await jumpTo(wv);
    picked = wv.picked;
    accept.disabled = reject.disabled = false;
    BUSY = false; spin.disabled = false;
  };
  accept.onclick = async () => {
    if (!picked) return;
    const r = await API.post('/api/accept', { key: picked });
    if (r.accepted) addSys(`✅ 定了：${picked}`);
    if (r.note) addSys(r.note);
    await refresh();
  };
  reject.onclick = async () => {
    if (!picked) return;
    const r = await API.post('/api/reject', { key: picked });
    if (r.note) addSys(r.note);
    await refresh();
  };
}

/* 格子跳动：80~120ms 起，逐渐减速，约 28 次后停在目标格 */
async function jumpTo(wv) {
  const cells = [...document.querySelectorAll('.cell')];
  if (!cells.length) return;
  const n = cells.length;
  const jumps = 28;
  const target = wv.pickedIndex;
  let idx = ((target - jumps) % n + n) % n;
  let delay = 80;
  for (let k = 0; k < jumps; k++) {
    cells.forEach(c => c.classList.remove('hot'));
    const cur = cells[idx];
    if (cur) {
      cur.classList.add('hot');
      if (k > jumps - 3) cur.scrollIntoView({ block: 'nearest' });
    }
    await sleep(delay);
    delay = Math.min(210, delay + 5);
    idx = (idx + 1) % n;
  }
  cells.forEach(c => { c.classList.remove('hot'); c.classList.add('dim'); });
  const land = cells[target];
  if (land) {
    land.classList.remove('dim');
    land.classList.add('land');
    land.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  addSys(`转到「${wv.picked}」，接受还是换一个？`);
}

/* ---------- 名片 + 四条 ---------- */
async function landOnCard(r) {
  addSys(`定了：${r.accepted}。下面四条是同一条件下的横向对比。`);
}

function renderCard(stage, st) {
  const c = st.card;
  stage.appendChild(el('h2', 'stage-title', '这张名片，看着还行吗？'));
  const card = el('div', 'card');
  card.innerHTML = `
    <img src="${esc(c.img || '/plate/' + (c.id || '') + '.svg')}" alt="${esc(c.name)}">
    <div class="card-body">
      <h3 class="card-name">${esc(c.name)}</h3>
      <div class="card-shop">${esc(c.shop || '')}</div>
      <div class="card-facts">
        <span class="fact">￥<b>${c.price}</b></span>
        <span class="fact">走 <b>${c.walk_min || 1}</b> 分钟</span>
        <span class="fact">⭐ <b>${c.rating}</b></span>
      </div>
      <p class="card-desc">${esc(c.desc || '')}</p>
    </div>`;
  stage.appendChild(card);

  const row = el('div', 'row');
  const ok = el('button', 'btn primary', '就它了，收尾');
  const no = el('button', 'btn ghost', '换一个');
  row.appendChild(ok); row.appendChild(no);
  stage.appendChild(row);
  ok.onclick = finalize;
  no.onclick = async () => { await API.post('/api/again'); await refresh(); };

  if (st.four && st.four.groups && st.four.groups.length) {
    stage.appendChild(el('p', 'stage-sub',
      `同一条件下另有 ${Math.max(0, st.four.groups.length - 1)} 条备选，往下滑就是四条并排对比`));
    renderFour(stage, st);
  }
}

function fourTitle(four) {
  const n = (four.groups || []).length;
  if (four.keyword && four.matched === false) {
    return `没有「${four.keyword}」，换成这些？`;
  }
  if (four.keyword && four.matched) {
    return `「${four.keyword}」有 ${n} 个，挑一个`;
  }
  return n >= 4 ? '四条并排，你挑一个' : `符合条件的就这 ${n} 条，挑一个`;
}

function renderFour(stage, st) {
  const four = st.four;
  stage.appendChild(el('h2', 'stage-title', fourTitle(four)));
  if (four.note) stage.appendChild(el('p', 'stage-sub', esc(four.note)));
  stage.appendChild(fourGrid(four));
}

function renderFourStage(stage, st) {
  const four = st.four;
  stage.appendChild(el('h2', 'stage-title', fourTitle(four)));
  if (four.note) stage.appendChild(el('p', 'stage-sub', esc(four.note)));
  stage.appendChild(fourGrid(four));
  const row = el('div', 'row');
  const again = el('button', 'btn ghost', '还是转个盘吧');
  again.onclick = startWheel;
  row.appendChild(again);
  stage.appendChild(row);
}

function fourGrid(four) {
  const box = el('div', 'four');
  four.groups.forEach(g => {
    const it = g.item;
    const b = el('button', 'pick' + (g.picked ? ' sel' : ''));
    b.innerHTML = `
      <div class="pick-head"><span class="badge">${g.emoji} ${esc(g.label)}</span></div>
      <div class="pick-name">${esc(it.name)}</div>
      <div class="pick-shop">${esc(it.shop || '')}</div>
      <div class="pick-reason">${esc(g.reason || '')}</div>
      <div class="pick-facts"><span>￥<b>${it.price}</b></span><span>走 <b>${it.walk_min || 1}</b> 分</span><span>⭐<b>${it.rating}</b></span></div>`;
    b.onclick = async () => {
      const r = await API.post('/api/select', { id: it.id });
      if (r.card) addSys(`换成「${r.card.name}」（${r.card.shop}）`);
      await refresh();
    };
    box.appendChild(b);
  });
  return box;
}

/* ---------- 收尾卡片 ---------- */
function renderFinal(stage, st) {
  const f = st.final;
  stage.appendChild(el('h2', 'stage-title', '今天的决定，贴这儿了'));
  const card = el('div', 'final');
  card.innerHTML = `
    <img src="${esc(f.img || ('/plate/' + (f.id || '') + '.svg'))}" alt="${esc(f.name)}">
    <div class="final-body">
      <div class="final-kicker">今日打卡 · ${esc(f.meal)}</div>
      <h3 class="final-name">${esc(f.name)}</h3>
      <div class="final-shop">${esc(f.shop || '')} · ${esc(f.where || '')}</div>
      <div class="card-facts">
        <span class="fact">￥<b>${f.price}</b></span>
        <span class="fact">走 <b>${f.walk_min}</b> 分钟</span>
        <span class="fact">⭐ <b>${f.rating}</b></span>
      </div>
      <p class="final-cheer">「${esc(f.cheer)}」</p>
      <div class="final-foot"><span>${esc(f.date)}</span><span>${f.cheer_from === 'model' ? '文案：模型生成' : '文案：模板兜底'}</span></div>
    </div>`;
  stage.appendChild(card);

  const row = el('div', 'row');
  const copy = el('button', 'btn primary', '复制文案');
  const shot = el('button', 'btn ghost', '截图提示');
  const again = el('button', 'btn ghost', '再选一次');
  row.appendChild(copy); row.appendChild(shot); row.appendChild(again);
  stage.appendChild(row);
  stage.appendChild(el('p', 'tip', esc(f.saved ? '已落盘：outputs/' + (f.saved.match(/(今日决定|就近决定)[^\\/]*\.md/) || [''])[0] : '')));

  const text = `${f.date} 今日打卡｜${f.shop}·${f.name}，人均 ¥${f.price}，走 ${f.walk_min} 分钟。${f.cheer}`;
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      addSys('文案已复制，去群里发吧 📋');
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      addSys('文案已复制，去群里发吧 📋');
    }
  };
  shot.onclick = () => addSys('截图提示：Win + Shift + S 框选这张卡片；手机拍屏也行，卡片是固定 340×470 的尺寸。');
  again.onclick = async () => {
    await API.post('/api/reset');
    $('#chatLog').innerHTML = '';
    addSys('再来一轮。今天想好吃啥了不？');
    await refresh();
  };
}

/* ============ 动作 ============ */
async function startWheel() {
  await API.post('/api/start');
  await refresh();
  addSys('好，先定这顿吃什么类型。');
}

async function finalize() {
  const r = await API.post('/api/finalize');
  if (r.note && !r.final) addSys(r.note);
  if (r.final) addSys('收尾卡片做好了 👇');
  await refresh();
}

async function send() {
  const v = $('#input').value.trim();
  if (!v || BUSY) return;
  $('#input').value = '';
  addMsg('user', v);
  BUSY = true;
  try {
    const r = await API.post('/api/chat', { text: v });
    (r.messages || []).forEach(m => {
      if (m.role === 'assistant') addAssistant(m);
      if (m.role === 'four') addSys('四条方案已生成，右边看。');
      if (m.role === 'wheel') addSys('转盘来了，右边看。');
    });
    await refresh();
  } catch (e) {
    addSys('请求失败了：' + e.message);
  } finally {
    BUSY = false;
  }
}

/* ============ 一键演示 ============
   早期版本只是把服务端状态一步步推过去，画面直接跳到结果，看起来像"假演示"。
   现在改成：在真实画布上把每一次转动播完再推进 —— 演示过程 = 真人操作的录放。 */

async function demoTurnTo(key, ms = 2200) {
  const ctx = WHEEL_CTX;
  if (!ctx || !ctx.canvas || !ctx.canvas.isConnected) return false;
  const opts = ctx.options || [];
  const i = opts.findIndex(o => o.key === key);
  if (i < 0) return false;
  const n = opts.length;
  const target = -(i + 0.5) * (Math.PI * 2 / n) + Math.PI * 8;
  await spinAnim(ctx.canvas, opts, target, ms);
  drawWheel(ctx.canvas, opts, target, i);
  syncProbs({ options: opts, scene: ctx.scene, pickedIndex: i });
  const legend = document.querySelectorAll('.prob')[i];
  if (legend) legend.classList.add('hit');
  addSys(`指针停在「${key}」`);
  return true;
}

async function demoAccept(key) {
  const r = await API.post('/api/accept', { key });
  if (r.note) addSys(r.note);
  await refresh();
  return r;
}

async function runDemo() {
  if (BUSY) return;
  BUSY = true;
  DEMO_STOP = false;
  const btn = $('#btnDemo');
  btn.textContent = '■ 停止演示';
  btn.onclick = () => { DEMO_STOP = true; };
  let step = 0;
  const TOTAL = 8;
  const tell = t => { step += 1; addSys(`[${step}/${TOTAL}] ${t}`); };

  try {
    await API.post('/api/reset');
    await API.post('/api/start');
    await refresh();
    addSys('▶ 一键演示开始：每一层都真的转、真的算，拒绝之后概率会真的重排。');
    await sleep(700);

    tell('第 0 层 · 餐别：正餐 / 甜品饮料，两个扇区各 50%');
    await demoTurnTo('正餐'); await sleep(800);
    if (DEMO_STOP) return;
    await demoAccept('正餐'); await sleep(700);

    tell('第 1 层 · 场景：食堂 / 外面吃');
    await demoTurnTo('食堂'); await sleep(800);
    if (DEMO_STOP) return;
    await demoAccept('食堂'); await sleep(700);

    tell('第 2 层 · 口味：辣 / 不辣，各 50%（「都行」是盘外按钮，不占扇区）');
    await demoTurnTo('辣'); await sleep(800);
    if (DEMO_STOP) return;
    await demoAccept('辣'); await sleep(900);

    const st0 = STATE || {};
    const poolN = (st0.wheel && st0.wheel.items || []).length;
    tell(`第 3 层 · 菜品格子：从 ${poolN || '这'} 道里真随机抽一次，看它逐格跳`);
    let s = await API.post('/api/spin');
    if (s.wheel) { await jumpTo(s.wheel); }
    await sleep(1000);
    if (DEMO_STOP) return;

    const first = (s.wheel || {}).picked || '刚才那道';
    tell(`拒绝「${first}」：它的权重清零，剩下的重新归一化`);
    await API.post('/api/reject', { key: first });
    await refresh();
    await sleep(1500);
    if (DEMO_STOP) return;

    tell('再抽一次（被拒的那道已经不在池子里）');
    s = await API.post('/api/spin');
    if (s.wheel) { await jumpTo(s.wheel); }
    await sleep(1000);
    if (DEMO_STOP) return;

    tell(`接受「${(s.wheel || {}).picked || ''}」，出美食名片 + 四条并列对比`);
    await API.post('/api/accept', { key: (s.wheel || {}).picked });
    await refresh();
    const gs = ((STATE && STATE.four && STATE.four.groups) || []);
    addSys(`右边现在 = 名片 + ${gs.length} 条并列方案（${gs.map(g => g.label).join(' / ')}），`
      + '这一步就是「转盘之后的多条方案」，停 5 秒看清再继续。');
    await sleep(5200);
    if (DEMO_STOP) return;

    tell('让模型现写一句打卡文案，并把今天的选择落盘');
    await API.post('/api/finalize');
    await refresh();
    addSys('✅ 演示结束。上面每一次转动、每一次概率变化，都是后端真算出来的。');
  } catch (e) {
    addSys('演示中断：' + e.message);
  } finally {
    BUSY = false;
    DEMO_STOP = false;
    btn.textContent = '▶ 一键演示';
    btn.onclick = runDemo;
  }
}

/* ============ 聊天气泡 ============ */
function addMsg(role, text) {
  const wrap = el('div', 'msg ' + role);
  wrap.appendChild(el('div', 'bubble', esc(text)));
  $('#chatLog').appendChild(wrap);
  scrollLog();
}

function addSys(text) {
  const wrap = el('div', 'msg sys');
  wrap.appendChild(el('div', 'bubble', esc(text)));
  $('#chatLog').appendChild(wrap);
  scrollLog();
}

function addAssistant(m) {
  const wrap = el('div', 'msg assistant');
  const b = el('div', 'bubble', esc(m.content));
  const calls = m.calls || [];
  if (calls.length) {
    const d = el('details', 'calls');
    const sum = el('summary', null, `工具调用 ${calls.length} 次（真实 tool_calls）`);
    d.appendChild(sum);
    calls.forEach(c => {
      const line = el('div', 'call');
      line.innerHTML = `<b>${esc(c.name)}</b>(${esc(JSON.stringify(c.args || {}))})<span class="res">${esc(String(c.result || '').slice(0, 200))}</span>`;
      d.appendChild(line);
    });
    b.appendChild(d);
  }
  if (m.fallback) {
    const tag = el('div', 'tag-fallback', '兜底模式');
    tag.style.display = 'inline-block';
    tag.style.marginTop = '8px';
    b.appendChild(tag);
  }
  wrap.appendChild(b);
  $('#chatLog').appendChild(wrap);
  scrollLog();
}

function scrollLog() {
  const box = $('#chatLog');
  box.scrollTop = box.scrollHeight;
}
