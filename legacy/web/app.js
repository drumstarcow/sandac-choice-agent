/* 吃饱饱助手 · 网页版智能体对话
   前端只负责展示：所有概率、工具调用、层级推进都由后端真实计算。 */

const PALETTE = [
  [240, 101, 58], [255, 169, 58], [247, 201, 72], [127, 200, 169],
  [106, 169, 233], [201, 154, 224], [239, 143, 168], [143, 191, 106],
  [232, 168, 124], [120, 190, 200]
];

const stream = document.getElementById('stream');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const quick = document.getElementById('quick');

let wheelEl = null;       // 当前转盘卡片 DOM
let wheelState = null;    // 当前转盘数据
let rotation = 0;         // 转盘累计旋转角度
let busy = false;

/* ---------------- 基础工具 ---------------- */
async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const data = await r.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function scrollDown() {
  const doIt = () => { stream.scrollTop = stream.scrollHeight; };
  requestAnimationFrame(doIt);
  setTimeout(doIt, 80);
  setTimeout(doIt, 420);
  setTimeout(doIt, 900);
}

/* ---------------- 消息渲染 ---------------- */
function addUser(text) {
  const m = el('div', 'msg user');
  m.appendChild(el('div', 'avatar', '🧑‍🎓'));
  m.appendChild(el('div', 'bubble', esc(text)));
  stream.appendChild(m);
  scrollDown();
}

function addAssistant(content, tools, allowHtml) {
  const m = el('div', 'msg');
  m.appendChild(el('div', 'avatar', '🍱'));
  const bubble = el('div', 'bubble', allowHtml ? content : esc(content));
  if (tools && tools.calls && tools.calls.length) {
    bubble.appendChild(renderTools(tools));
  }
  m.appendChild(bubble);
  stream.appendChild(m);
  scrollDown();
}

function renderTools(tools) {
  const box = el('div', 'tools');
  const head = el('div', 'tools-head');
  head.innerHTML = `<span class="caret">▶</span><span>工具调用</span>
    <span class="count">${tools.calls.length} 次 · 点击展开</span>`;
  head.onclick = () => box.classList.toggle('open');
  box.appendChild(head);

  const body = el('div', 'tools-body');
  tools.calls.forEach(c => {
    const item = el('div', 'tool-call');
    const argStr = JSON.stringify(c.args || {});
    item.innerHTML =
      `<div class="tool-name">${esc(c.name)}(${esc(argStr.slice(0, 220))})</div>` +
      `<div class="tool-res clip">↳ ${esc((c.result || '').slice(0, 520))}</div>`;
    body.appendChild(item);
  });
  if (tools.notes && tools.notes.length) {
    tools.notes.forEach(n => {
      body.appendChild(el('div', 'tool-note', esc(n)));
    });
  }
  box.appendChild(body);
  return box;
}

function addNote(text) {
  stream.appendChild(el('div', 'note', esc(text)));
  scrollDown();
}

function showTyping() {
  const m = el('div', 'msg');
  m.appendChild(el('div', 'avatar', '🍱'));
  m.appendChild(el('div', 'typing', '<i></i><i></i><i></i>'));
  stream.appendChild(m);
  scrollDown();
  return m;
}

/* ---------------- 转盘卡片 ---------------- */
function makeCanvas() {
  const c = document.createElement('canvas');
  c.id = 'wheelCanvas';
  return c;
}

function drawWheel(canvas, options, pickedKey) {
  const n = options.length;
  const dpr = window.devicePixelRatio || 1;
  const size = 262;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);

  const cx = size / 2, cy = size / 2, R = size / 2 - 5;
  const probs = options.map(o => o.prob || 0);
  const pmin = Math.min(...probs), pmax = Math.max(...probs);
  const norm = p => (pmax - pmin < 1e-9 ? 1 : (p - pmin) / (pmax - pmin));

  // 外圈底
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = '#fff4ea';
  ctx.fill();

  const step = (Math.PI * 2) / n;
  for (let i = 0; i < n; i++) {
    const a0 = -Math.PI / 2 + i * step;
    const a1 = a0 + step;
    const [r, g, b] = PALETTE[i % PALETTE.length];
    const k = norm(options[i].prob || 0);
    const alpha = 0.42 + 0.58 * k;           // 概率越高越浓

    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, R, a0, a1);
    ctx.closePath();
    ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#fff';
    ctx.stroke();

    if (options[i].key === pickedKey) {
      ctx.save();
      ctx.lineWidth = 4;
      ctx.strokeStyle = '#2b2119';
      ctx.stroke();
      ctx.restore();
    }

    // 扇区文字（扇区不多时才画）；左半圈翻转 180° 保证不倒着读
    if (n <= 9) {
      const mid = (a0 + a1) / 2;
      const mm = ((mid % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
      const flip = mm > Math.PI / 2 && mm < Math.PI * 1.5;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(mid + (flip ? Math.PI : 0));
      ctx.textAlign = flip ? 'left' : 'right';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = 'rgba(43,33,25,.92)';
      ctx.font = `600 ${n <= 5 ? 15 : 12.5}px "PingFang SC","Microsoft YaHei",sans-serif`;
      const label = options[i].key.length > 6 ? options[i].key.slice(0, 6) : options[i].key;
      ctx.fillText(label, flip ? -(R - 14) : (R - 14), 0);
      ctx.restore();
    }
  }

  // 中心白圈（给按钮垫底）
  ctx.beginPath();
  ctx.arc(cx, cy, 48, 0, Math.PI * 2);
  ctx.fillStyle = '#fff';
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#f0e5d8';
  ctx.stroke();
}

function renderLegend(options) {
  const box = el('div', 'legend');
  options.forEach((o, i) => {
    const [r, g, b] = PALETTE[i % PALETTE.length];
    const item = el('div', 'legend-item');
    item.innerHTML =
      `<span class="legend-dot" style="background:rgb(${r},${g},${b})"></span>` +
      `${esc(o.key)} <span class="legend-pct">${(o.prob * 100).toFixed(1)}%</span>`;
    box.appendChild(item);
  });
  return box;
}

function buildWheelCard(wheel) {
  const card = el('div', 'wheelcard');

  const title = el('div', 'wheel-title', esc(wheel.title || '转盘'));
  const sub = el('div', 'wheel-sub', '');
  card.appendChild(title);
  card.appendChild(sub);

  const stage = el('div', 'wheel-stage');
  stage.appendChild(el('div', 'pointer'));
  const canvas = makeCanvas();
  stage.appendChild(canvas);

  const spinBtn = el('button', 'spinbtn', '转一下<small>点中间</small>');
  spinBtn.onclick = () => doSpin(card);
  stage.appendChild(spinBtn);
  card.appendChild(stage);

  card.appendChild(renderLegend(wheel.options));

  const picked = el('div', 'picked-hint');
  picked.style.display = 'none';
  card.appendChild(picked);

  const controls = el('div', 'wheel-controls');
  const okBtn = el('button', 'ok', '✓ 就这个');
  const noBtn = el('button', 'no', '✕ 换一个');
  okBtn.disabled = true;
  noBtn.disabled = true;
  okBtn.onclick = () => doAccept(card);
  noBtn.onclick = () => doReject(card);
  controls.appendChild(noBtn);
  controls.appendChild(okBtn);
  card.appendChild(controls);

  const bar = el('div', 'budgetbar');
  const sel = document.createElement('select');
  wheel.budgetOptions.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = v === '不限' ? '💰 金额不限' : '💰 ≤ ' + v + ' 元';
    if (v === wheel.budget) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = async () => {
    sel.disabled = true;
    const res = await api('/api/budget', { value: sel.value });
    sel.disabled = false;
    if (res.wheel) {
      wheelState = res.wheel;
      updateWheelCard(res.wheel);
    } else {
      resetRotation();
      card.remove(); wheelEl = null;
    }
    if (res.note) addNote(res.note);
  };
  bar.appendChild(sel);
  card.appendChild(bar);

  card._canvas = canvas;
  card._spinBtn = spinBtn;
  card._okBtn = okBtn;
  card._noBtn = noBtn;
  card._picked = picked;
  card._legend = card.querySelector('.legend');
  card._sel = sel;
  card._sub = sub;

  resetRotation();
  drawWheel(canvas, wheel.options, null);
  return card;
}

function resetRotation() {
  rotation = 0;
  if (wheelEl && wheelEl._canvas) {
    wheelEl._canvas.style.transition = 'none';
    wheelEl._canvas.style.transform = 'rotate(0deg)';
  }
}

function updateWheelCard(wheel) {
  if (!wheelEl) return;
  wheelEl._sub.textContent = wheel.scene === 'dish'
    ? `已拒绝的不会再出现 · 当前 ${wheel.options.length} 道候选`
    : '概率真实计算 · 拒掉的不会再出现';
  drawWheel(wheelEl._canvas, wheel.options, null);
  wheelEl._legend.replaceWith(renderLegend(wheel.options));
  wheelEl._legend = wheelEl.querySelector('.legend');
  wheelEl._picked.style.display = 'none';
  wheelEl._okBtn.disabled = true;
  wheelEl._noBtn.disabled = true;
  wheelEl._spinBtn.disabled = false;
  wheelEl._spinBtn.innerHTML = '转一下<small>点中间</small>';
}

function placeWheelCard(wheel) {
  if (wheelEl) { wheelEl.remove(); wheelEl = null; }
  wheelState = wheel;
  const card = buildWheelCard(wheel);
  wheelEl = card;
  stream.appendChild(card);
  updateWheelCard(wheel);
  scrollDown();
}

async function doSpin(card) {
  if (busy || !wheelState || !wheelState.options.length) return;
  busy = true;
  card._spinBtn.disabled = true;
  card._okBtn.disabled = true;
  card._noBtn.disabled = true;
  card._picked.style.display = 'none';

  let res;
  try {
    res = await api('/api/spin', {});
  } catch (e) {
    addNote('转盘出错了：' + e.message);
    busy = false;
    return;
  }
  const w = res.wheel;
  if (!w) { busy = false; return; }

  const n = wheelState.options.length;      // 用抽之前的角度分布
  const center = (w.pickedIndex + 0.5) * (360 / n);
  const targetMod = ((360 - center) % 360 + 360) % 360;
  const cur = ((rotation % 360) + 360) % 360;
  let delta = targetMod - cur;
  if (delta < 0) delta += 360;
  rotation += delta + 360 * 6;

  const canvas = card._canvas;
  canvas.style.transition = 'transform 3.2s cubic-bezier(.16,.74,.12,1)';
  canvas.style.transform = `rotate(${rotation}deg)`;

  setTimeout(() => {
    // 抽中后权重已 ×0.6，概率真的变了
    wheelState.options = w.options;
    drawWheel(canvas, w.options, w.picked);
    card._legend.replaceWith(renderLegend(w.options));
    card._legend = card.querySelector('.legend');

    card._picked.style.display = 'block';
    const p = (w.options[w.pickedIndex] || {}).prob || 0;
    card._picked.innerHTML =
      `🎯 转到 <b>${esc(w.picked)}</b> · 权重 ×0.6，现在 ${(p * 100).toFixed(1)}%`;

    card._spinBtn.innerHTML = '再转<small>点中间</small>';
    card._spinBtn.disabled = false;
    card._okBtn.disabled = false;
    card._noBtn.disabled = false;
    busy = false;
    scrollDown();
  }, 3300);
}

async function doAccept(card) {
  if (busy || !wheelState) return;
  const pickedEl = card._picked;
  const m = pickedEl.innerHTML.match(/转到 <b>(.+?)<\/b>/);
  if (!m) return;
  const key = m[1].replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');

  busy = true;
  card._okBtn.disabled = true;
  card._noBtn.disabled = true;
  card._spinBtn.disabled = true;

  let res;
  try {
    res = await api('/api/accept', { key });
  } catch (e) {
    addNote('出错了：' + e.message);
    busy = false;
    return;
  }

  if (res.wheel) {
    // 推进到下一层
    wheelEl.remove();
    wheelEl = null;
    wheelState = null;
    addAssistant(`好，「${key}」定了 👌`, null);
    placeWheelCard(res.wheel);
  } else if (res.card) {
    wheelEl.remove();
    wheelEl = null;
    wheelState = null;
    addCardView(res.card);
  }
  busy = false;
  scrollDown();
}

async function doReject(card) {
  if (busy || !wheelState) return;
  const m = card._picked.innerHTML.match(/转到 <b>(.+?)<\/b>/);
  if (!m) return;
  const key = m[1].replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');

  busy = true;
  card._okBtn.disabled = true;
  card._noBtn.disabled = true;
  card._spinBtn.disabled = true;

  let res;
  try {
    res = await api('/api/reject', { key });
  } catch (e) {
    addNote('出错了：' + e.message);
    busy = false;
    return;
  }

  if (!res.wheel) {
    wheelEl.remove(); wheelEl = null; wheelState = null;
    addNote(res.note || '所有选项都被拒了。');
    busy = false;
    return;
  }

  // 扇区少了，角度重排 → 转盘归零再画
  rotation = 0;
  card._canvas.style.transition = 'none';
  card._canvas.style.transform = 'rotate(0deg)';
  wheelState = res.wheel;
  updateWheelCard(res.wheel);
  if (res.note) addNote(res.note);
  busy = false;
}

/* ---------------- 推荐卡片 ---------------- */
function addRecommend(rec, budgetOptions) {
  const card = el('div', 'reccard');
  const head = el('div', 'rec-head');
  head.innerHTML = `<span class="t">📍 ${esc(rec.campus || '')} 就近推荐</span>
    <span class="s">以校区为圆心 · 距离+性价比+评分 加权排序</span>`;
  card.appendChild(head);

  if (!rec.items || !rec.items.length) {
    card.appendChild(el('div', 'rec-foot', '这个条件下没找到合适的店，换个口味或放宽预算试试。'));
    stream.appendChild(card);
    scrollDown();
    return;
  }

  rec.items.forEach((it, i) => {
    const row = el('div', 'rec-row' + (i === 0 ? ' best' : ''));
    row.innerHTML = `
      <div class="rec-rank">${i === 0 ? '🏆' : i + 1}</div>
      <div class="rec-main">
        <div class="rec-name">${esc(it.name)}${i === 0 ? '<span class="tag">最划算</span>' : ''}</div>
        <div class="rec-meta">
          <span>📍 ${esc(it.address || '')}</span>
          <span>🚶 ${it.distance_km} km</span>
          <span>⭐ ${it.rating}</span>
          ${it.signature ? `<span>🍽 ${esc(it.signature)}</span>` : ''}
        </div>
      </div>
      <div class="rec-price">￥${it.avg_price}<small>综合 ${it.score ?? ''}</small></div>`;
    row.onclick = () => pickShop(it, card);
    card.appendChild(row);
  });

  card.appendChild(el('div', 'rec-foot', '点任意一家即可把今天定下来 · 数据为示例，以实际为准'));
  stream.appendChild(card);
  scrollDown();
}

async function pickShop(it, card) {
  card.querySelectorAll('.rec-row').forEach(r => r.style.pointerEvents = 'none');
  let res;
  try {
    res = await api('/api/pick', {
      name: it.name, address: it.address, distance_km: it.distance_km,
      avg_price: it.avg_price, rating: it.rating,
      signature: it.signature || '', tags: it.tags || []
    });
  } catch (e) {
    addNote('出错了：' + e.message);
    return;
  }
  addAssistant(`就它了！${it.name}，￥${it.avg_price}，走 🏃`, null);
  addNote(res.content || '已记录今天的决定。');
  refreshSide();
  scrollDown();
}

/* ---------------- 名片卡片 ---------------- */
function addCardView(d) {
  const card = el('div', 'cardcard');
  const top = el('div', 'card-top');
  const place = [d.canteen, d.window].filter(Boolean).join(' · ') || '—';
  top.innerHTML = `<div class="card-price">￥${d.price ?? '?'}</div>
    <div class="card-emoji">🍜</div>
    <div class="card-name">${esc(d.name)}</div>`;
  card.appendChild(top);

  const body = el('div', 'card-body');
  body.innerHTML = `
    <div class="row"><span class="k">在哪吃</span><span>${esc(place)}</span></div>
    ${d.desc ? `<div class="row"><span class="k">一句话</span><span>${esc(d.desc)}</span></div>` : ''}
    <div class="card-tags">${(d.tags || []).map(t => `<span>${esc(t)}</span>`).join('')}</div>`;
  card.appendChild(body);

  const actions = el('div', 'card-actions');
  const ok = el('button', 'ok', '✓ 就吃这个');
  const again = el('button', '', '换一个菜');
  ok.onclick = async () => {
    ok.disabled = true; again.disabled = true;
    const res = await api('/api/finalize', {});
    addAssistant('定啦！今天的饱饱问题解决了 🎉', null);
    addNote(res.content || '已生成今日决定。');
    refreshSide();
  };
  again.onclick = async () => {
    ok.disabled = true; again.disabled = true;
    const res = await api('/api/again', {});
    card.remove();
    if (res.wheel) placeWheelCard(res.wheel);
  };
  actions.appendChild(again);
  actions.appendChild(ok);
  card.appendChild(actions);

  stream.appendChild(card);
  scrollDown();
}

/* ---------------- 侧栏 ---------------- */
async function refreshSide() {
  try {
    const s = await (await fetch('/api/state')).json();
    renderProfile(s.profile);
    renderRecent(s.recent, s.history);
    const mn = document.getElementById('modelname');
    if (mn && s.model) mn.textContent = s.model;
  } catch (e) {
    const box = document.getElementById('profile');
    if (box) box.innerHTML = '<div class="empty">画像加载失败：' + esc(e.message) + '</div>';
  }
}

function renderProfile(p) {
  const box = document.getElementById('profile');
  if (!p) {
    box.innerHTML = '<div class="empty">画像加载失败</div>';
    return;
  }
  const arr = v => Array.isArray(v) ? v : (v ? [v] : []);
  const rows = [
    ['校区', p.campus || '未设置'],
    ['金额上限', p.budget_cap && p.budget_cap !== '不限' ? '￥' + p.budget_cap : '不限'],
    ['口味', arr(p.taste_prefs).join('、') || '未设置'],
    ['忌口', arr(p.dislikes).join('、') || '无'],
  ];
  box.innerHTML = rows.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><span>${esc(v)}</span></div>`).join('');
}

function renderRecent(recent, hist) {
  const box = document.getElementById('recent');
  if (!hist || !hist.length) {
    box.innerHTML = '<div class="empty">还没有记录。<br>选一次就有了，我会从里面学你的口味。</div>';
    return;
  }
  box.innerHTML = hist.slice().reverse().map(h => {
    const name = h.dish || h.shop_name || '—';
    const t = (h.ts || '').slice(5);
    return `<div class="hist-item"><span class="h-name">${esc(name)}</span><span class="h-time">${esc(t)}</span></div>`;
  }).join('');
}

/* ---------------- 对话主流程 ---------------- */
async function send(text) {
  text = (text || '').trim();
  if (!text || busy) return;
  busy = true;
  input.value = '';
  sendBtn.disabled = true;
  addUser(text);
  const typing = showTyping();

  let res;
  try {
    res = await api('/api/chat', { text });
  } catch (e) {
    typing.remove();
    addNote('请求失败：' + e.message + '（检查本地模型是否在运行）');
    busy = false; sendBtn.disabled = false;
    return;
  }
  typing.remove();

  (res.messages || []).forEach(m => {
    if (m.role === 'assistant') {
      addAssistant(m.content, m.tools);
    } else if (m.role === 'wheel') {
      placeWheelCard(m.wheel);
    } else if (m.role === 'recommend') {
      addRecommend(m.recommend, m.budgetOptions);
    }
  });

  busy = false;
  sendBtn.disabled = false;
  input.focus();
  refreshSide();
}

/* ---------------- 启动 ---------------- */
async function boot() {
  await refreshSide();

  // 智能体主动开场
  addAssistant(
    '今天想好吃啥了不？😋<br>' +
    '<span style="color:#9b8b7e;font-size:13px">有想法就直接告诉我（比如「想吃火锅」「来份牛肉面」）；' +
    '没想法就说声「随便」，我把选择权交给命运 🎰</span>', null, true);
}

sendBtn.onclick = () => send(input.value);
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
quick.addEventListener('click', e => {
  const b = e.target.closest('.chip');
  if (b) send(b.dataset.q);
});
document.getElementById('newchat').onclick = async () => {
  await api('/api/reset', {});
  stream.innerHTML = '';
  wheelEl = null; wheelState = null; rotation = 0;
  boot();
  refreshSide();
};

boot();

/* 截图/演示辅助：?auto=wheel|idea|spin 自动走一段（不影响正常使用） */
const AUTO = new URLSearchParams(location.search).get('auto');

function whenReady(sel, cb) {
  const timer = setInterval(() => {
    const e = document.querySelector(sel);
    if (e) { clearInterval(timer); cb(e); }
  }, 400);
  setTimeout(() => clearInterval(timer), 90000);
}

if (AUTO) {
  setTimeout(() => {
    send(AUTO === 'idea' ? '我在金海校区，我想吃火锅' : '还没想好，你帮我选吧');
  }, 400);
  if (AUTO === 'spin') {
    whenReady('.wheelcard .spinbtn', btn => {
      setTimeout(() => btn.click(), 300);
    });
  }
  if (AUTO === 'full') {
    let rounds = 0;
    const tick = () => {
      if (rounds > 14) return;
      rounds++;
      const card = document.querySelector('.wheelcard');
      if (card) {
        const pick = card.querySelector('.picked-hint');
        const ok = card.querySelector('.wheel-controls .ok');
        const sp = card.querySelector('.spinbtn');
        if (pick && pick.style.display !== 'none' && ok && !ok.disabled) {
          ok.click();
        } else if (sp && !sp.disabled) {
          sp.click();
        }
      }
      setTimeout(tick, 900);
    };
    setTimeout(tick, 800);
  }
}
