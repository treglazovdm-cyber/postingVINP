const $ = s => document.querySelector(s);
const names = {vk:'ВКонтакте',youtube:'YouTube',instagram:'Instagram',telegram:'Telegram'};
const states = {scheduled:'Запланировано',blocked:'Нужны аккаунты',cancelled:'Отменено',sent:'Отправлено',partial:'Отправлено частично',failed:'Ошибка отправки',sending:'Отправляется'};
let posts = [];
async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Не удалось выполнить запрос');
  return data;
}
function element(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function feedback(message) { $('#feedback').textContent = message; }
function archived(post,platform='telegram') { return (post.archives || []).includes(platform); }
function render() {
  const integrationsView = location.hash === '#integrations';
  $('#integrations-view').hidden = !integrationsView;
  $('#planner-workspace').hidden = integrationsView;
  $('#integrations-tab').setAttribute('aria-pressed', String(integrationsView));
  const archivePlatform = location.hash === '#vk-archive' ? 'vk' : 'telegram';
  const archiveView = ['#telegram-archive','#vk-archive'].includes(location.hash);
  $('#archive-count').textContent = posts.filter(p => archived(p,'telegram')).length;
  $('#plan-tab').setAttribute('aria-pressed', String(!archiveView && !integrationsView));
  $('#archive-tab').setAttribute('aria-pressed', String(archiveView && archivePlatform === 'telegram'));
  $('#vk-archive-tab').setAttribute('aria-pressed', String(archiveView && archivePlatform === 'vk'));
  $('#vk-archive-count').textContent = posts.filter(p => archived(p,'vk')).length;
  $('#plan-heading').textContent = archiveView ? `Архив ${names[archivePlatform]}` : 'Контент-план';
  $('#archive-description').hidden = !archiveView;
  $('#filter').hidden = archiveView;
  $('#post-form').closest('.editor').hidden = archiveView;
  $('#planner-workspace').classList.toggle('archive-view', archiveView);
  const archiveList = $('#posts'); archiveList.replaceChildren();
  archiveList.hidden = !archiveView;
  $('#plan-columns').hidden = archiveView;
  const items = [];
  const statusFor = (post, platform) => (post.deliveries || []).find(d => d.platform === platform)?.status || post.status;
  const archivePosts = posts.filter(p => archived(p,archivePlatform));
  if (archiveView) {
    if (!archivePosts.length) archiveList.append(element('div','Архив пока пуст. Перенесите сюда опубликованные или отменённые записи.','empty'));
    archivePosts.forEach(post => items.push({post, list:archiveList, platform:archivePlatform}));
  }
  for (const platform of ['telegram','vk']) {
    const list = $(`#${platform}-posts`); list.replaceChildren();
    const candidates = posts.filter(p => !archived(p,platform) && p.platforms.includes(platform));
    const visible = candidates.filter(p => $('#filter').value === 'all' || statusFor(p,platform) === $('#filter').value);
    $(`#${platform}-count`).textContent = visible.length;
    if (!visible.length) list.append(element('div',`Нет записей для ${names[platform]}. Выберите площадку при создании публикации или измените фильтр.`,'empty'));
    if (!archiveView) visible.forEach(post => items.push({post,list,platform}));
  }
  for (const {post,list,platform} of items) {
    const card = element('article', undefined, `card ${post.status}`);
    const date = element('time', new Date(post.scheduled_at).toLocaleString('ru-RU'));
    date.dateTime = post.scheduled_at;
    card.append(date, element('h3',post.title), element('p',post.text));
    const tags = element('div',undefined,'tags');
    post.platforms.forEach(p => tags.append(element('span',names[p],'tag')));
    const shownStatus = platform ? statusFor(post,platform) : post.status;
    tags.append(element('span',states[shownStatus] || shownStatus,'status')); card.append(tags);
    if (post.media_name) card.append(element('p',`Файл: ${post.media_name}`));
    const platformDelivery = (post.deliveries || []).find(d => d.platform === platform);
    if (platformDelivery?.message || (!platform && post.message)) card.append(element('p', platformDelivery?.message || post.message,'error'));
    else if (!(post.deliveries || []).length && post.message) card.append(element('p',post.message,'error'));
    for (const delivery of (post.deliveries || []).filter(d => !platform || d.platform === platform)) {
      card.append(element('p', `${names[delivery.platform]}: ${states[delivery.status] || delivery.status}${delivery.message_id ? ' · ID сообщения: ' + delivery.message_id : ''}`));
    }
    if (['scheduled','blocked'].includes(post.status)) {
      const cancel = element('button','Отменить'); cancel.type='button';
      cancel.onclick = async () => {
        try { await api(`/api/cancel/${post.id}`,{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}); await refresh(); }
        catch(e) { feedback(e.message); }
      }; card.append(cancel);
    }
    const published = (post.deliveries || []).find(d => d.platform === platform && d.status === 'sent' && d.message_id);
    if (published || post.status === 'cancelled' || archived(post,platform)) {
      const action = element('button', archived(post,platform) ? 'Вернуть в журнал' : `В архив ${names[platform]}`); action.type = 'button';
      const result = element('p', '', 'error'); result.setAttribute('role', 'status');
      action.onclick = async () => {
        action.disabled = true;
        try {
          const operation = archived(post,platform) ? 'unarchive' : 'archive';
          await api(`/api/${operation}/${post.id}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform})});
          await refresh();
        } catch(e) { result.textContent = e.message; }
        finally { action.disabled = false; }
      };
      card.append(action, result);
    }
    if (platform && post.platforms.length > 1) card.append(element('small','Отмена действует на обе площадки. Перенос в архив — только на выбранную.'));
    list.append(card);
  }
}
async function refresh() { posts = await api('/api/posts'); render(); }
async function loadMedia(selected) {
  const media = await api('/api/media');
  $('#media').replaceChildren(new Option('Без файла',''));
  media.forEach(m => $('#media').add(new Option(m.name,m.id)));
  if(selected) $('#media').value=selected;
}
$('#timezone').textContent = `Часовой пояс устройства: ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
$('#filter').onchange=render;
$('#upload').onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  if (!file.size || file.size > 100*1024*1024) { feedback('Выберите непустой файл размером до 100 МБ.'); e.target.value=''; return; }
  const submit = $('#post-form button[type=submit]'); submit.disabled=true;
  feedback('Загружаем файл…');
  try {
    const result = await api('/api/media',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name)},body:file});
    await loadMedia(result.id); feedback('Файл загружен.');
  } catch(err) { feedback(err.message); }
  finally { submit.disabled=false; e.target.value=''; }
};
$('#post-form').onsubmit=async e => {
  e.preventDefault();
  const form = e.target, fields = new FormData(form);
  const platforms = fields.getAll('platform');
  if(!platforms.length) return feedback('Выберите хотя бы одну площадку.');
  const button = $('#post-form button[type=submit]'); button.disabled=true;
  try {
    await api('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:fields.get('title'),text:fields.get('text'),platforms,preview_mode:$('#preview-mode').value,media_id:$('#media').value,scheduled_at:new Date(fields.get('date')).toISOString()})});
    form.reset(); resetCover(); feedback('Запись добавлена. Telegram отправит её по расписанию, если подключён.'); await refresh();
  } catch(err) { feedback(err.message); }
  finally { button.disabled=false; }
};
Promise.all([refresh(),loadMedia()]).catch(e=>feedback(e.message));
setInterval(()=>refresh().catch(e=>feedback(e.message)),15000);

function paintConnection(data) {
  const descriptions = {verified:['✓ Подключение проверено','verified'],error:['✕ Ошибка проверки','error'],unchecked:['○ Требуется проверка','neutral'],unconfigured:['○ Не подключено','neutral'],stale:['○ Проверьте повторно','neutral']};
  const [label, style] = descriptions[data.connection_status] || descriptions.unchecked;
  $('#telegram-status').textContent = data.connection_status === 'verified'
    ? '✓ Telegram: подключение проверено. ВК пока не подключён.'
    : `Telegram: ${label.replace(/^[^А-Яа-я]+/, '')}. ВК пока не подключён.`;
  $('#telegram-badge').textContent = label;
  $('#telegram-badge').className = `connection-badge ${style}`;
  $('#telegram-checked-at').textContent = data.checked_at ? `Последняя проверка: ${new Date(data.checked_at).toLocaleString('ru-RU')}. Статус проверки действует 24 часа.` : '';
}
async function refreshConnection() {
  try { paintConnection(await api('/api/telegram')); }
  catch(e) { paintConnection({connection_status:'error'}); }
}
async function telegramStatus() {
  const data = await api('/api/telegram');
  paintConnection(data);
  $('#telegram-chat').value = data.chat_id || '';
  $('#telegram-token').required = !data.token_saved;
  $('#telegram-token-hint').textContent = data.token_saved
    ? 'Токен сохранён. Оставьте поле пустым, чтобы сохранить текущий, или введите новый.'
    : 'Введите токен, выданный официальным @BotFather.';
}
$('#telegram-check').onclick = async () => {
  const button = $('#telegram-check'); button.disabled = true; $('#telegram-save').disabled = true;
  try {
    const result = await api('/api/telegram/check', {method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});
    $('#telegram-result').textContent = result.message;
  } catch(e) { $('#telegram-result').textContent = e.message; }
  finally { await refreshConnection(); button.disabled = false; $('#telegram-save').disabled = false; }
};
telegramStatus().catch(e => { $('#telegram-status').textContent = e.message; });

$('#telegram-form').onsubmit = async e => {
  e.preventDefault();
  const button = $('#telegram-save'); button.disabled = true;
  $('#telegram-check').disabled = true;
  try {
    await api('/api/telegram/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:$('#telegram-token').value, chat_id:$('#telegram-chat').value})});
    $('#telegram-token').value = '';
    await telegramStatus();
    $('#telegram-result').textContent = 'Настройки сохранены. Теперь проверьте подключение. Сообщения не отправлялись.';
  } catch(e) { $('#telegram-result').textContent = e.message; }
  finally { button.disabled = false; $('#telegram-check').disabled = false; }
};

let coverUrl;
function resetCover() {
  if (coverUrl) URL.revokeObjectURL(coverUrl);
  coverUrl = null;
  $('#cover-image').hidden = true;
  $('#cover-image').removeAttribute('src');
  $('#cover-result').textContent = '';
  $('#cover-tools').hidden = $('#preview-mode').value !== 'text';
}
$('#preview-mode').onchange = resetCover;
$('#post-form textarea[name=text]').addEventListener('input', resetCover);
$('#post-form input[name=title]').addEventListener('input', resetCover);
$('#generate-cover').onclick = async () => {
  const button = $('#generate-cover'); button.disabled = true;
  try {
    const text = $('#post-form textarea[name=text]').value.trim() || $('#post-form input[name=title]').value.trim();
    const response = await fetch('/api/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
    if (!response.ok) throw new Error((await response.json()).error);
    const image = await response.blob();
    resetCover(); coverUrl = URL.createObjectURL(image);
    $('#cover-image').src = coverUrl; $('#cover-image').hidden = false;
    $('#cover-result').textContent = 'Превью готово. При публикации обложка будет создана из сохранённого текста.';
  } catch(e) { $('#cover-result').textContent = e.message; }
  finally { button.disabled = false; }
};

$('#plan-tab').onclick = () => { location.hash = 'plan'; };
$('#archive-tab').onclick = () => { location.hash = 'telegram-archive'; };
window.addEventListener('hashchange', render);

$('#vk-archive-tab').onclick = () => { location.hash = 'vk-archive'; };

$('#integrations-tab').onclick = () => { location.hash = 'integrations'; };
render();

setInterval(refreshConnection,60000);
for (const field of ['#telegram-token','#telegram-chat']) {
  $(field).addEventListener('input', () => {
    $('#telegram-badge').textContent = '○ Есть несохранённые изменения';
    $('#telegram-badge').className = 'connection-badge neutral';
  });
}
