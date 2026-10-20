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
function render() {
  const list = $('#posts'); list.replaceChildren();
  const visible = posts.filter(p => $('#filter').value === 'all' || p.status === $('#filter').value);
  if (!visible.length) list.append(element('div','Здесь появятся ваши публикации. Добавьте первую запись в контент-план.','empty'));
  for (const post of visible) {
    const card = element('article', undefined, `card ${post.status}`);
    const date = element('time', new Date(post.scheduled_at).toLocaleString('ru-RU'));
    date.dateTime = post.scheduled_at;
    card.append(date, element('h3',post.title), element('p',post.text));
    const tags = element('div',undefined,'tags');
    post.platforms.forEach(p => tags.append(element('span',names[p],'tag')));
    tags.append(element('span',states[post.status] || post.status,'status')); card.append(tags);
    if (post.media_name) card.append(element('p',`Файл: ${post.media_name}`));
    if (post.message) card.append(element('p',post.message,'error'));
    for (const delivery of post.deliveries || []) {
      card.append(element('p', `${names[delivery.platform]}: ${states[delivery.status] || delivery.status}${delivery.message_id ? ' · ID сообщения: ' + delivery.message_id : ''}`));
    }
    if (['scheduled','blocked'].includes(post.status)) {
      const cancel = element('button','Отменить'); cancel.type='button';
      cancel.onclick = async () => {
        try { await api(`/api/cancel/${post.id}`,{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}); await refresh(); }
        catch(e) { feedback(e.message); }
      }; card.append(cancel);
    }
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
    await api('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:fields.get('title'),text:fields.get('text'),platforms,media_id:$('#media').value,scheduled_at:new Date(fields.get('date')).toISOString()})});
    form.reset(); feedback('Запись добавлена. Telegram отправит её по расписанию, если подключён.'); await refresh();
  } catch(err) { feedback(err.message); }
  finally { button.disabled=false; }
};
Promise.all([refresh(),loadMedia()]).catch(e=>feedback(e.message));
setInterval(()=>refresh().catch(e=>feedback(e.message)),15000);

async function telegramStatus() {
  const data = await api('/api/telegram');
  $('#telegram-status').textContent = data.configured
    ? 'Telegram: настройки заданы. Проверьте подключение перед планированием. ВК пока не подключён.'
    : 'Telegram не настроен: заполните настройки интеграции ниже. ВК пока не подключён.';
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
  finally { button.disabled = false; $('#telegram-save').disabled = false; }
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
