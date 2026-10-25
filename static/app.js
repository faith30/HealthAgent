'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {data: null, csrf: '', settingsLoaded: false, remindersLoaded: false, busy: false, toastTimer: null, imageAttachment: null, imageLoading: false, imageVersion: 0};
const pages = {overview: '照护概览', chat: '对话试用', records: '健康记录', reminders: '提醒计划', settings: '家庭设置'};
const contexts = {fasting: '空腹', postmeal2h: '餐后2小时', premeal: '餐前', random: '随机', unknown: '未注明'};
const targetKeys = ['bp_systolic_high', 'bp_diastolic_high', 'bp_systolic_low', 'bp_diastolic_low', 'glucose_fasting_high', 'glucose_postmeal_high'];
const sourceNames = {test: '本地试用', demo: '本地试用', wechat: '微信', manual: '手动录入', scheduler: '提醒任务', test_scheduler: '测试提醒'};

function el(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function displayDate(value, short = false) {
  if (!value) return '时间未注明';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', year: short ? undefined : 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
}
function list(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') { try { const p = JSON.parse(value); return Array.isArray(p) ? p : []; } catch (_) { return []; } }
  return [];
}
function setPill(target, text, kind = 'gray') { target.textContent = text; target.className = `pill pill-${kind}`; }
function showToast(message, isError = false) {
  const box = $('#toast');
  box.textContent = message; box.className = `toast ${isError ? 'error' : 'success'}`; box.hidden = false;
  clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { box.hidden = true; }, isError ? 7000 : 4200);
}
function imageLimit() { return Math.min(Number(state.data?.features?.max_image_bytes) || 8388608, 8388608); }
function imageReadiness() {
  if (!state.data) return '尚未连接本地服务，请稍后再试。';
  if (state.data.features?.image_upload !== true) return '当前服务尚未提供图片上传，请更新并重启本地服务。';
  if (state.data.features?.image_processing_available === false) return '本地图片处理组件尚未就绪，请按使用说明完成安装。';
  if (!state.data.config?.vision?.enabled) return '图片识别尚未启用，请到「家庭设置 → 照片识别」配置。';
  if (!state.data.config?.vision?.has_effective_key) return '识图接口还没有可用密钥，请先保存独立密钥或配置同源复用。';
  return '';
}
function renderImageState() {
  const file = state.imageAttachment;
  $('#image-preview').hidden = !file;
  if (file) { $('#image-preview-img').src = file.dataURL; $('#image-file-name').textContent = file.name; $('#image-file-size').textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB · 尚未发送`; }
  else { $('#image-preview-img').removeAttribute('src'); $('#image-file-name').textContent = ''; $('#image-file-size').textContent = ''; }
  const reason = imageReadiness();
  $('#image-availability').textContent = state.imageLoading ? '正在本机读取图片…' : reason || `识图已配置：${state.data.config.vision.model || '自定义模型'}。发送前请核对预览。`;
  $('#image-availability').classList.toggle('not-ready', Boolean(reason));
  $('#image-limit').textContent = `JPEG / PNG / WebP · 单张不超过 ${(imageLimit() / 1024 / 1024).toFixed(0)} MB`;
  $('#attach-image').disabled = state.busy || state.imageLoading;
  $('#image-input').disabled = state.busy || state.imageLoading;
  $('#remove-image').disabled = state.busy;
}
function clearImage() { state.imageVersion += 1; state.imageAttachment = null; state.imageLoading = false; $('#image-input').value = ''; renderImageState(); }
async function chooseImage(file) {
  if (!file || state.busy) return;
  const version = ++state.imageVersion;
  try {
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('请选择 JPEG、PNG 或 WebP 图片。');
    if (!file.size || file.size > imageLimit()) throw new Error(`图片需大于 0 字节且不超过 ${(imageLimit() / 1024 / 1024).toFixed(0)} MB。`);
    state.imageLoading = true; renderImageState();
    const dataURL = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('图片读取失败，请重新选择。')); reader.readAsDataURL(file); });
    if (state.imageVersion !== version) return;
    state.imageAttachment = {dataURL, name: file.name, size: file.size};
  } catch (error) { if (state.imageVersion === version) { state.imageAttachment = null; $('#image-input').value = ''; showToast(error.message || '图片未能读取。', true); } }
  finally { if (state.imageVersion === version) { state.imageLoading = false; renderImageState(); } }
}
function updateVisionReuseNote() {
  const form = $('#vision-form'); const reuse = field(form, 'reuse_text_key').checked;
  field(form, 'api_key').disabled = reuse;
  const note = $('#vision-origin-note'); let sameOrigin = false;
  try { sameOrigin = new URL(field(form, 'base_url').value.trim()).origin === new URL(state.data?.config?.model?.base_url || '').origin; } catch (_) { /* Incomplete URLs are explained below and validated on save. */ }
  note.textContent = reuse ? (sameOrigin ? '接口同源，可以复用已保存在本机的文字模型密钥。' : '目前接口不同源或地址不完整，不能复用密钥。请核对地址，或取消复用并填写独立密钥。') : '将使用独立识图密钥。只有接口协议、域名和端口一致时，才可以选择复用。';
  note.classList.toggle('field-warning', reuse && !sameOrigin);
  return !reuse || sameOrigin;
}
async function api(path, body) {
  const options = {method: body === undefined ? 'GET' : 'POST', cache: 'no-store', headers: {'Accept': 'application/json'}};
  if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.headers['X-CSRF-Token'] = state.csrf; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); } catch (_) { throw new Error('服务返回了无法读取的内容，请查看本地运行窗口。'); }
  if (!response.ok || result.error) throw new Error(typeof result.error === 'string' ? result.error : result.message || `请求没有完成（${response.status}）。`);
  return result;
}
async function withButton(button, action) {
  if (button.disabled) return;
  button.disabled = true; button.setAttribute('aria-busy', 'true');
  try { return await action(); } catch (error) { showToast(error.message || '操作没有完成，请稍后重试。', true); }
  finally { button.disabled = false; button.removeAttribute('aria-busy'); }
}
function goPage(name, focus = false) {
  if (!pages[name]) name = 'overview';
  $$('.nav-item').forEach(button => { const selected = button.dataset.tab === name; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; });
  $$('.tab-panel').forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
  $('#page-title').textContent = pages[name];
  history.replaceState(null, '', `#${name}`);
  window.scrollTo(0, 0);
  if (focus) $(`#tab-${name}`).focus();
}
function empty(container, title, message, icon = '○') {
  const box = el('div', 'empty-state'); box.append(el('span', 'empty-icon', icon), el('strong', '', title), el('p', '', message)); container.append(box);
}
function measurementText(item) {
  if (item.kind === 'bp') return `血压 ${item.systolic ?? '—'}/${item.diastolic ?? '—'} ${item.unit || 'mmHg'}`;
  return `${contexts[item.context] || '未注明餐次'}血糖 ${item.value ?? '—'} ${item.unit || '单位待确认'}`;
}
function renderPending(container, pending) {
  const active = pending.filter(p => !p.confirmed_at && !p.rejected_at && !['confirmed', 'rejected'].includes(p.status));
  const signature = JSON.stringify(active);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  const drafts = new Map($$('.pending-item', container).map(card => [String(card.dataset.id), {time: $('.pending-time', card)?.value || '', context: $('.pending-context', card)?.value || ''}]));
  container.replaceChildren();
  if (!active.length) { empty(container, '目前没有待确认的读数', '收到读数后，会先请你核对数值、单位和测量时间。', '✓'); return; }
  for (const record of active) {
    const card = el('div', 'pending-item'); card.dataset.id = record.id;
    const fromImage = record.input_type === 'image';
    card.append(el('div', 'pending-source', `${sourceNames[record.source] || '记录'} · ${displayDate(record.created_at, true)}`));
    for (const item of list(record.items)) card.append(el('div', 'pending-reading', measurementText(item)));
    if (record.text) card.append(el('div', 'small', `原话：${record.text}`));
    if (fromImage) card.append(el('div', 'pending-warning', `这条记录来自照片。请确认它属于${state.data?.config?.profile?.name || '爸爸'}本人，并核对原图上的数值；其他人的测量请不要保存。`));
    const warnings = list(record.warnings);
    if (warnings.length) card.append(el('div', 'pending-warning', warnings.join('\n')));
    const times = [...new Set(list(record.items).map(i => i.measured_at).filter(Boolean))];
    if (times.length) card.append(el('div', 'small', `测量时间：${times.map(t => displayDate(t)).join('；')}`));
    const corrections = el('div', 'pending-corrections'); const draft = drafts.get(String(record.id)) || {};
    let actualTime = null; let mealContext = null;
    if (list(record.items).some(item => !item.measured_at)) {
      actualTime = el('input', 'pending-time'); actualTime.type = 'datetime-local'; actualTime.required = true; actualTime.value = draft.time || '';
      corrections.append(inputLabel('补充实际测量时间（北京时间）', actualTime));
    }
    if (list(record.items).some(item => item.kind === 'glucose' && (!item.context || item.context === 'unknown'))) {
      mealContext = el('select', 'pending-context'); mealContext.required = true;
      for (const [value, label] of [['', '请选择测量情境'], ['fasting', '空腹'], ['postmeal2h', '餐后2小时'], ['premeal', '餐前'], ['random', '随机测量'], ...(!fromImage ? [['unknown', '确实不清楚']] : [])]) { const option = el('option', '', label); option.value = value; mealContext.append(option); }
      mealContext.value = draft.context || ''; corrections.append(inputLabel('补充血糖测量情境', mealContext));
    }
    if (corrections.childElementCount) card.append(corrections);
    const actions = el('div', 'action-row');
    const confirm = el('button', 'button button-primary button-small', fromImage ? `确认是${state.data?.config?.profile?.name || '爸爸'}的读数并保存` : '核对无误，确认记录'); confirm.type = 'button';
    confirm.addEventListener('click', () => withButton(confirm, async () => { if (actualTime && !actualTime.reportValidity()) return; if (mealContext && !mealContext.reportValidity()) return; const payload = {pending_id: record.id}; if (fromImage) payload.subject_confirmed = true; if (actualTime?.value) payload.measured_at = actualTime.value; if (mealContext?.value) payload.context = mealContext.value; await api('/api/confirm', payload); await loadState(); showToast('已确认并保存这组记录。'); }));
    const reject = el('button', 'button button-secondary button-small', '不准确，重新填写'); reject.type = 'button';
    reject.addEventListener('click', () => withButton(reject, async () => { await api('/api/reject', {pending_id: record.id}); await loadState(); goPage('chat'); $('#chat-input').value = record.text || ''; $('#chat-input').focus(); showToast('已撤下待确认记录，请修正后重新发送。'); }));
    actions.append(confirm, reject); card.append(actions); container.append(card);
  }
}
function renderAlerts(alerts) {
  const box = $('#alert-list'); box.replaceChildren();
  const active = alerts.filter(a => !a.acked_at && !a.acknowledged_at && !a.acknowledged && a.status !== 'acked');
  if (!active.length) { empty(box, '目前没有待处理提醒', '这只反映系统收到的记录，不表示已确认健康状态。', '♡'); return; }
  for (const item of active.slice(0, 8)) {
    const card = el('div', `alert-item ${item.level === 'urgent' ? 'urgent' : ''}`);
    card.append(el('small', '', `${item.level === 'urgent' ? '请立即关注' : '请家属关注'} · ${displayDate(item.created_at, true)} · ${sourceNames[item.source] || '提醒'}`));
    card.append(el('p', '', item.message || list(item.messages).join('\n') || '请查看这条记录。'));
    const ack = el('button', 'text-button', '我已看到并接手'); ack.type = 'button';
    ack.addEventListener('click', () => withButton(ack, async () => { await api('/api/alerts/ack', {id: item.id}); await loadState(); showToast('已标记为家属接手。'); }));
    card.append(ack); box.append(card);
  }
}
function renderReadings(measurements) {
  const sorted = [...measurements].sort((a, b) => String(b.measured_at || b.created_at || '').localeCompare(String(a.measured_at || a.created_at || '')));
  const bp = sorted.find(m => m.kind === 'bp'); const glucose = sorted.find(m => m.kind === 'glucose');
  $('#latest-bp').replaceChildren(document.createTextNode(bp ? `${bp.systolic}/${bp.diastolic}` : '—'), el('small', '', 'mmHg'));
  $('#latest-glucose').replaceChildren(document.createTextNode(glucose ? String(glucose.value) : '—'), el('small', '', glucose?.unit || 'mmol/L'));
  $('#latest-bp-time').textContent = bp ? `${sourceNames[bp.source] || bp.source || '手动录入'} · ${displayDate(bp.measured_at || bp.created_at, true)}` : '暂无已确认记录';
  $('#latest-glucose-time').textContent = glucose ? `${sourceNames[glucose.source] || glucose.source || '手动录入'} · ${contexts[glucose.context] || '餐次未注明'} · ${displayDate(glucose.measured_at || glucose.created_at, true)}` : '暂无已确认记录';
  renderRecords();
}
function renderRecords() {
  const records = list(state.data?.measurements); const filter = $('#record-filter').value;
  const visible = records.filter(m => filter === 'all' || m.kind === filter).sort((a, b) => String(b.measured_at || b.created_at || '').localeCompare(String(a.measured_at || a.created_at || '')));
  $('#records-body').replaceChildren(); $('#records-empty').replaceChildren(); $('#record-total').textContent = `${visible.length} 条已确认记录`;
  if (!visible.length) empty($('#records-empty'), '还没有这类记录', '在对话试用中录入并确认一组读数，就会出现在这里。', '▤');
  for (const item of visible) {
    const tr = el('tr');
    tr.append(el('td', '', displayDate(item.measured_at || item.created_at)), el('td', '', item.kind === 'bp' ? '血压' : '血糖'), el('td', 'value-cell', item.kind === 'bp' ? `${item.systolic}/${item.diastolic} ${item.unit || 'mmHg'}` : `${item.value} ${item.unit || ''}`), el('td', '', item.kind === 'bp' ? '—' : contexts[item.context] || '未注明'), el('td', '', sourceNames[item.source] || item.source || '手动录入'));
    $('#records-body').append(tr);
  }
}
function renderOutbox(outbox) {
  const box = $('#outbox-list'); box.replaceChildren();
  if (!outbox.length) { empty(box, '暂时没有发送任务', '提醒和回复任务会在这里展示；试用任务与真实微信任务会区分标注。', '↗'); return; }
  const statusMap = {queued: ['等待发送', 'sand'], pending: ['等待发送', 'sand'], sent: ['已发送', 'green'], sending: ['发送中', 'sand'], failed: ['发送失败', 'red'], error: ['发送失败', 'red'], uncertain: ['发送结果待核实', 'red'], test: ['测试草稿', 'gray'], draft: ['测试草稿', 'gray'], test_only: ['测试草稿', 'gray'], cancelled: ['已取消', 'gray']};
  for (const item of [...outbox].slice(0, 40)) {
    const row = el('div', 'outbox-item'); const content = el('div', 'outbox-content'); const info = statusMap[item.status] || [item.status || '未发送', 'gray'];
    const pill = el('span'); setPill(pill, info[0], info[1]); row.append(pill);
    content.append(el('small', '', `${displayDate(item.created_at, true)} · ${sourceNames[item.source] || item.source || '任务'}${item.group_name ? ` · ${item.group_name}` : ''}`), el('p', '', item.text || item.message || item.body || ''));
    if (item.error) content.append(el('small', '', `错误：${item.error}`));
    row.append(content); box.append(row);
  }
}
function renderNext(reminders) {
  const enabled = reminders.filter(r => r.enabled).sort((a, b) => String(a.time).localeCompare(String(b.time)));
  $('#next-reminder-list').replaceChildren();
  if (!enabled.length) { $('#next-title').textContent = '还没有启用的提醒'; $('#next-detail').textContent = '先按医嘱安排适合爸爸的测量时间。'; return; }
  $('#next-title').textContent = `${enabled.length} 项每日提醒`; $('#next-detail').textContent = '时间按北京时间（上海）执行';
  for (const item of enabled.slice(0, 3)) { const row = el('div', 'mini-line'); row.append(el('span', 'mini-time', item.time), el('span', '', item.label || ({bp: '测量血压', glucose: '测量血糖', tip: '饮食小贴士', menu: '今日菜单'}[item.kind] || '日常提醒'))); $('#next-reminder-list').append(row); }
}
function field(form, name) { return form.elements.namedItem(name); }
function populateSettings(config) {
  const profile = config.profile || {}; const profileForm = $('#profile-form');
  for (const name of ['name', 'age', 'stroke_months', 'swallowing', 'renal_status', 'allergies', 'medications', 'doctor_plan']) { const input = field(profileForm, name); if (input) input.value = profile[name] ?? (name === 'swallowing' ? 'unknown' : name === 'renal_status' ? 'unknown' : ''); }
  const targets = config.doctor_targets || {}; for (const name of targetKeys) field($('#targets-form'), name).value = targets[name] ?? '';
  const model = config.model || {}; for (const name of ['base_url', 'model']) field($('#model-form'), name).value = model[name] || '';
  field($('#model-form'), 'enabled').checked = Boolean(model.enabled); field($('#model-form'), 'api_key').value = '';
  const vision = config.vision || {}; for (const name of ['base_url', 'model']) field($('#vision-form'), name).value = vision[name] || '';
  field($('#vision-form'), 'enabled').checked = Boolean(vision.enabled); field($('#vision-form'), 'reuse_text_key').checked = Boolean(vision.reuse_text_key); field($('#vision-form'), 'api_key').value = '';
  updateVisionReuseNote();
  const wechat = config.wechat || {}; for (const name of ['group_name', 'patient_sender', 'caregiver_sender']) field($('#wechat-form'), name).value = wechat[name] || '';
  field($('#wechat-form'), 'live_enabled').checked = Boolean(wechat.live_enabled);
  const meal = config.meal_plan || {}; field($('#meal-plan-form'), 'confirmed').checked = Boolean(meal.confirmed);
  $('#meal-plan-editor').replaceChildren();
  for (const [index, day] of ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日'].entries()) { const input = el('textarea'); input.name = `day_${index}`; input.rows = 3; input.maxLength = 2000; input.placeholder = '早餐、午餐、晚餐及已确认的份量；可先留空'; input.value = list(meal.days)[index] || ''; $('#meal-plan-editor').append(inputLabel(day, input)); }
  state.settingsLoaded = true;
}
function inputLabel(text, input) { const label = el('label', '', text); label.append(input); return label; }
function reminderEditorItem(reminder = {}) {
  const row = el('div', 'reminder-row'); row.dataset.id = reminder.id || `reminder_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const head = el('div', 'reminder-row-head'); const enabled = el('input'); enabled.type = 'checkbox'; enabled.className = 'reminder-enabled'; enabled.checked = reminder.enabled !== false;
  const enabledLabel = el('label', 'check-line'); enabledLabel.append(enabled, el('span', '', '启用这条提醒'));
  const remove = el('button', 'text-button remove-reminder', '移除'); remove.type = 'button'; remove.addEventListener('click', () => { row.remove(); }); head.append(enabledLabel, remove);
  const fields = el('div', 'reminder-row-fields');
  const time = el('input', 'reminder-time'); time.type = 'time'; time.required = true; time.value = reminder.time || '08:00';
  const kind = el('select', 'reminder-kind'); for (const [value, text] of [['bp', '血压测量'], ['glucose', '血糖测量'], ['tip', '饮食小贴士'], ['menu', '每日菜单']]) { const option = el('option', '', text); option.value = value; kind.append(option); } kind.value = reminder.kind || 'bp';
  const label = el('input', 'reminder-label'); label.maxLength = 100; label.required = true; label.value = reminder.label || ''; label.placeholder = '例如：到了约定的测量时间';
  const grace = el('input', 'reminder-grace'); grace.type = 'number'; grace.min = '5'; grace.max = '720'; grace.step = '5'; grace.value = reminder.grace_minutes ?? 60;
  fields.append(inputLabel('每天的时间', time), inputLabel('提醒类型', kind), inputLabel('提醒内容', label), inputLabel('漏报等待（分钟）', grace));
  function updateGrace() { grace.disabled = ['tip', 'menu'].includes(kind.value); } kind.addEventListener('change', updateGrace); updateGrace();
  row.append(head, fields); $('#reminder-editor').append(row);
}
function populateReminders(reminders) { $('#reminder-editor').replaceChildren(); reminders.forEach(reminderEditorItem); if (!reminders.length) empty($('#reminder-editor'), '还没有设置每日提醒', '点击“新增提醒”，按医嘱填写时间和内容。', '◷'); state.remindersLoaded = true; }
function render(data) {
  const config = data.config || {}; const profile = config.profile || {}; const connected = Boolean(data.bridge?.connected); const isLive = data.mode === 'live' || Boolean(config.wechat?.live_enabled);
  $('#server-dot').className = 'dot online'; $('#server-status').textContent = '本地服务已连接'; $('#connection-banner').hidden = true;
  setPill($('#mode-pill'), isLive ? '正式模式已启用' : '本地测试模式', isLive ? 'green' : 'sand'); setPill($('#bridge-pill'), connected ? '微信通道在线' : '微信未连接', connected ? 'green' : 'gray'); setPill($('#settings-bridge'), connected ? '接入程序在线' : '接入程序未连接', connected ? 'green' : 'gray');
  $('#mode-notice').textContent = isLive ? (connected ? '已允许正式微信收发。请留意任务状态；消息发送成功不代表家属已经接手。' : '已允许正式收发，但微信接入程序未连接。当前无法向家庭群发送消息。') : '当前为本地试用。读数可在本机保存，微信消息不会发送；请先验证流程，再连接家庭群。';
  $('#hero-profile').textContent = `${profile.name || '爸爸'}${profile.age ? ` · ${profile.age}岁` : ''}，把每天的血压、血糖和提醒记在一起。`;
  $('#avatar').textContent = (profile.name || '家').slice(0, 1);
  const stats = data.stats || {}; const pending = list(data.pending); const alerts = list(data.alerts); const measurements = list(data.measurements);
  $('#stat-today-label').textContent = measurements.some(item => ['test', 'demo'].includes(item.source)) ? '今日记录（含试用）' : '今日已确认记录';
  for (const [selector, count] of [['#stat-today', stats.today_count ?? 0], ['#stat-pending', stats.pending_count ?? pending.length], ['#stat-alerts', stats.alert_count ?? alerts.filter(a => !a.acked_at).length]]) $(selector).replaceChildren(document.createTextNode(String(count)), el('small', '', selector === '#stat-alerts' ? '项' : '条'));
  $('#pending-badge').textContent = String(stats.pending_count ?? pending.length);
  renderNext(list(config.reminders)); renderPending($('#overview-pending'), pending); renderReadings(measurements); renderAlerts(alerts); renderOutbox(list(data.outbox));
  const modelReady = Boolean(config.model?.enabled && config.model?.has_key);
  $('#chat-model-label').textContent = modelReady ? `已配置云端模型 · ${config.model.model || '自定义模型'}` : '本地演示 · 未启用云端模型';
  setPill($('#model-key-status'), config.model?.has_key ? '已保存密钥' : '未配置密钥', config.model?.has_key ? 'green' : 'gray');
  const vision = config.vision || {};
  setPill($('#vision-key-status'), vision.has_effective_key ? (vision.reuse_text_key ? '正在复用文字密钥' : '已保存识图密钥') : '识图缺少可用密钥', vision.has_effective_key ? 'green' : 'gray');
  if (!state.settingsLoaded) populateSettings(config);
  if (!state.remindersLoaded) populateReminders(list(config.reminders));
  renderImageState();
}
async function loadState() {
  const data = await api('/api/state'); state.data = data; state.csrf = data.csrf || ''; render(data); return data;
}
function reportDisconnected(error) {
  $('#server-dot').className = 'dot offline'; $('#server-status').textContent = '本地服务连接中断';
  const banner = $('#connection-banner'); banner.hidden = false; banner.textContent = `暂时无法连接本地服务，页面数据可能不是最新状态。${error.message || ''}`;
}
function chatBubble(text, role = 'assistant') {
  const message = el('div', `chat-message ${role}`); message.append(el('div', 'bubble', text));
  if (role === 'user') message.append(el('span', 'bubble-label', '本地试用输入'));
  $('#chat-thread').append(message); $('#chat-thread').scrollTop = $('#chat-thread').scrollHeight; return message;
}
async function saveForm(form, payload, text) {
  await withButton($('button[type="submit"]', form), async () => { await api('/api/config', payload); state.settingsLoaded = false; await loadState(); showToast(text); });
}

document.addEventListener('DOMContentLoaded', async () => {
  $('#today-label').textContent = new Date().toLocaleDateString('zh-CN', {timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'long'});
  $$('.nav-item').forEach(button => button.addEventListener('click', () => goPage(button.dataset.tab)));
  $$('[data-go]').forEach(button => button.addEventListener('click', () => goPage(button.dataset.go)));
  $('.brand').addEventListener('click', event => { event.preventDefault(); goPage('overview'); });
  $('.navigation').addEventListener('keydown', event => { if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const buttons = $$('.nav-item'); const current = buttons.indexOf(document.activeElement); const direction = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1; const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (current + direction + buttons.length) % buttons.length; goPage(buttons[next].dataset.tab, true); });
  goPage(location.hash.slice(1));
  $('#record-filter').addEventListener('change', renderRecords);
  $$('[data-example]').forEach(button => button.addEventListener('click', () => { $('#chat-input').value = button.dataset.example; $('#chat-input').focus(); }));
  $('#attach-image').addEventListener('click', () => $('#image-input').click());
  $('#image-input').addEventListener('change', event => chooseImage(event.currentTarget.files?.[0]));
  $('#remove-image').addEventListener('click', clearImage);
  $('#chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('#chat-form').requestSubmit(); } });
  $('#chat-form').addEventListener('submit', async event => {
    event.preventDefault(); const text = $('#chat-input').value.trim(); const attachment = state.imageAttachment;
    if ((!text && !attachment) || state.busy || state.imageLoading) return;
    if (attachment && imageReadiness()) { showToast(imageReadiness(), true); return; }
    state.busy = true; renderImageState(); const button = $('#chat-send'); button.disabled = true; button.textContent = attachment ? '正在识图…' : '正在处理…';
    chatBubble(attachment ? `照片：${attachment.name}${text ? `\n${text}` : ''}` : text, 'user'); $('#chat-input').value = '';
    try {
      const payload = {text, sender: state.data?.config?.profile?.name || '爸爸'};
      if (attachment) payload.image_base64 = attachment.dataURL;
      const result = await api(attachment ? '/api/image' : '/api/message', payload);
      const visionNeedsRetry = Boolean(attachment && (result.needs_correction || result.answer_source === 'vision_error'));
      const answer = chatBubble(result.reply || '已收到，请查看待确认记录。', visionNeedsRetry ? 'error' : 'assistant');
      if (attachment && result.answer_source === 'vision') answer.append(el('span', 'bubble-label', `云端识图${result.image?.width && result.image?.height ? ` · ${result.image.width} × ${result.image.height}` : ''} · 原图未保存在本地服务`));
      if (attachment && !visionNeedsRetry) clearImage();
      if (visionNeedsRetry) { $('#chat-input').value = text; showToast('识图尚未完成，照片和说明已保留，可以调整后重试。', true); }
      await loadState();
      if (result.pending_id) { const note = chatBubble(attachment ? `照片里的读数尚未正式保存。请在「照护概览 → 待核对读数」中确认属于${state.data?.config?.profile?.name || '爸爸'}本人、核对数值，并填写实际测量时间。` : '这组读数还没有正式保存，请在「照护概览 → 待核对读数」中确认。'); const go = el('button', 'button button-secondary button-small', '去核对读数'); go.type = 'button'; go.addEventListener('click', () => goPage('overview')); note.append(go); }
    }
    catch (error) { chatBubble(error.message || '本次处理失败，请重试。', 'error'); if (attachment) $('#chat-input').value = text; }
    finally { state.busy = false; button.disabled = false; button.textContent = '发送 ↑'; renderImageState(); $('#chat-thread').scrollTop = $('#chat-thread').scrollHeight; }
  });
  $('#profile-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const profile = {}; for (const name of ['name', 'age', 'stroke_months', 'swallowing', 'renal_status', 'allergies', 'medications', 'doctor_plan']) { const raw = field(form, name).value.trim(); profile[name] = ['age', 'stroke_months'].includes(name) ? (raw === '' ? null : Number(raw)) : raw; } await saveForm(form, {profile}, '家人资料已保存。'); });
  $('#targets-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const doctor_targets = {}; for (const name of targetKeys) { const raw = field(form, name).value.trim(); doctor_targets[name] = raw === '' ? null : Number(raw); } await saveForm(form, {doctor_targets}, '医嘱规则已保存。'); });
  $('#model-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const model = {enabled: field(form, 'enabled').checked, base_url: field(form, 'base_url').value.trim(), model: field(form, 'model').value.trim()}; const key = field(form, 'api_key').value.trim(); if (key) model.api_key = key; await saveForm(form, {model}, '模型配置已保存。'); });
  $('#clear-api-key').addEventListener('click', () => withButton($('#clear-api-key'), async () => { await api('/api/config', {clear_api_key: true, model: {enabled: false}}); state.settingsLoaded = false; await loadState(); showToast('已清除本机保存的 API 密钥，并关闭云端回答。'); }));
  field($('#vision-form'), 'reuse_text_key').addEventListener('change', updateVisionReuseNote);
  field($('#vision-form'), 'base_url').addEventListener('input', updateVisionReuseNote);
  $('#vision-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; if (!updateVisionReuseNote()) { showToast('接口不同源时不能复用文字密钥。请核对地址或改用独立密钥。', true); return; } const vision = {enabled: field(form, 'enabled').checked, reuse_text_key: field(form, 'reuse_text_key').checked, base_url: field(form, 'base_url').value.trim(), model: field(form, 'model').value.trim()}; const key = field(form, 'api_key').value.trim(); if (key && !vision.reuse_text_key) vision.api_key = key; await saveForm(form, {vision}, '识图配置已保存，文字模型配置保持不变。'); });
  $('#clear-vision-api-key').addEventListener('click', () => withButton($('#clear-vision-api-key'), async () => { await api('/api/config', {clear_vision_api_key: true, vision: {enabled: false}}); state.settingsLoaded = false; await loadState(); showToast('已清除独立识图密钥并关闭识图；文字模型密钥保持不变。'); }));
  $('#meal-plan-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const meal_plan = {confirmed: field(form, 'confirmed').checked, days: Array.from({length: 7}, (_, index) => field(form, `day_${index}`).value.trim())}; await saveForm(form, {meal_plan}, '一周菜单已保存。只有已核对且不为空的当天菜单会用于提醒。'); });
  $('#wechat-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const wechat = {live_enabled: field(form, 'live_enabled').checked}; for (const name of ['group_name', 'patient_sender', 'caregiver_sender']) wechat[name] = field(form, name).value.trim(); if (wechat.live_enabled && !state.data?.config?.wechat?.live_enabled) { if (!window.confirm('开启后，已连接的微信接入程序可以向指定家庭群发送真实消息。请确认群名和成员显示名已经核对，并已完成测试群验证。现在开启正式收发吗？')) return; } await saveForm(form, {wechat}, '微信设置已保存。请核对页面显示的实际连接状态。'); });
  $('#add-reminder').addEventListener('click', () => { $$('.empty-state', $('#reminder-editor')).forEach(item => item.remove()); reminderEditorItem({kind: 'bp', enabled: false, label: '到了约定的测量时间，请测量并报数。', time: '08:00', grace_minutes: 60}); });
  $('#reminders-form').addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const reminders = $$('.reminder-row').map(row => ({id: row.dataset.id, time: $('.reminder-time', row).value, label: $('.reminder-label', row).value.trim(), kind: $('.reminder-kind', row).value, enabled: $('.reminder-enabled', row).checked, grace_minutes: Number($('.reminder-grace', row).value || 60)})); await withButton($('button[type="submit"]', form), async () => { await api('/api/config', {reminders}); state.remindersLoaded = false; await loadState(); showToast('提醒计划已保存。'); }); });
  $('#check-scheduler').addEventListener('click', () => withButton($('#check-scheduler'), async () => { await api('/api/scheduler/run', {}); await loadState(); showToast('已检查当前到期任务。可在健康记录页查看测试发送任务。'); }));
  try { await loadState(); } catch (error) { reportDisconnected(error); }
  setInterval(() => { if (!document.hidden && !state.busy) loadState().catch(reportDisconnected); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadState().catch(reportDisconnected); });
});
