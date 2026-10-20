"""Telegram Bot API integration. Tokens never enter application responses/logs."""
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from urllib import request, error

import video

_settings_path = None
_settings_lock = threading.RLock()


def configure_storage(directory):
    global _settings_path
    _settings_path = Path(directory) / 'telegram-settings.json'


def credentials():
    with _settings_lock:
        saved = {}
        if _settings_path and _settings_path.exists():
            try:
                saved = json.loads(_settings_path.read_text())
                if not isinstance(saved, dict) or any(not isinstance(saved.get(k, ''), str) for k in ('token', 'chat_id')):
                    raise ValueError()
            except (OSError, ValueError):
                raise DeliveryError('Telegram: не удалось прочитать сохранённые настройки.') from None
        return (saved.get('token', os.environ.get('TELEGRAM_BOT_TOKEN', '')).strip(),
                saved.get('chat_id', os.environ.get('TELEGRAM_CHAT_ID', '')).strip())


def public_settings():
    token, chat = credentials()
    return {'configured': bool(token and chat), 'token_saved': bool(token), 'chat_id': chat}


def save_settings(payload):
    token = payload.get('token', '')
    chat = payload.get('chat_id', '')
    if not isinstance(token, str) or not isinstance(chat, str):
        raise ValueError('Telegram: поля должны содержать текст.')
    token, chat = token.strip(), chat.strip()
    if not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}|-\d{5,20}', chat):
        raise ValueError('Telegram: укажите @имя_канала или числовой ID вида -100…')
    if token and not re.fullmatch(r'\d{5,20}:[A-Za-z0-9_-]{20,200}', token):
        raise ValueError('Telegram: проверьте формат токена из BotFather.')
    with _settings_lock:
        token = token or credentials()[0]
        if not token:
            raise ValueError('Telegram: введите токен бота из BotFather.')
        if _settings_path is None:
            raise OSError('Storage is not initialized')
        temporary = None
        try:
            # Same filesystem + atomic replace: readers see either complete version.
            with tempfile.NamedTemporaryFile(mode='w', dir=_settings_path.parent, prefix='.telegram-', delete=False) as out:
                temporary = Path(out.name)
                os.chmod(temporary, 0o600)
                json.dump({'token': token, 'chat_id': chat}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, _settings_path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return public_settings()


class DeliveryError(Exception):
    pass


def configured():
    try:
        token, chat = credentials()
        return bool(token and chat)
    except DeliveryError:
        return False


def validate(text, media=None):
    # Telegram counts UTF-16 code units, including surrogate pairs for emoji.
    limit = 1024 if media else 4096
    if len(text.encode('utf-16-le')) // 2 > limit:
        raise ValueError(f'Telegram: максимум {limit} символов для текста или подписи.')
    if media and media['size'] > 49 * 1024 * 1024:
        raise ValueError('Telegram: файл должен быть не более 49 МБ.')
    if media and Path(media['name']).suffix.lower() in {'.jpg', '.jpeg', '.png'} and media['size'] > 10 * 1024 * 1024:
        raise ValueError('Telegram: фото должно быть не более 10 МБ.')


def api(method, fields, upload=None, account=None, extra_uploads=()):
    token, chat = account or credentials()
    if not token or not chat:
        raise DeliveryError('Telegram: токен бота или канал не настроены.')
    if upload:
        boundary = 'vinp' + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        for field, path, extension in (upload, *extra_uploads):
            mime = 'image/jpeg' if extension == '.jpg' else 'video/mp4' if extension == '.mp4' else 'application/octet-stream'
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{field}{extension}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            parts.extend([path.read_bytes(), b'\r\n'])
        parts.append(f'--{boundary}--\r\n'.encode())
        body, content_type = b''.join(parts), f'multipart/form-data; boundary={boundary}'
    else:
        body, content_type = json.dumps(fields).encode(), 'application/json'
    req = request.Request(f'https://api.telegram.org/bot{token}/{method}', data=body, headers={'Content-Type': content_type})
    try:
        with request.urlopen(req, timeout=120) as response:
            data = json.load(response)
    except error.HTTPError as exc:
        # Do not expose exception URLs, tokens or remote descriptions.
        messages = {400: 'Неверный канал, формат или параметры сообщения.',
                    401: 'Токен бота недействителен.', 403: 'Боту запрещена публикация. Проверьте права администратора.',
                    429: 'Лимит Telegram. Повторная отправка автоматически не выполняется.'}
        raise DeliveryError('Telegram: ' + messages.get(exc.code, 'Ошибка API. Проверьте канал перед повторной публикацией.')) from None
    except (OSError, ValueError):
        raise DeliveryError('Telegram: ошибка соединения. Результат неизвестен — проверьте канал перед повторной публикацией.') from None
    if not data.get('ok') or 'result' not in data:
        raise DeliveryError('Telegram: API не подтвердил выполнение запроса.')
    return data['result']


def check():
    account = credentials()
    bot = api('getMe', {}, account=account)
    member = api('getChatMember', {'chat_id': account[1], 'user_id': bot['id']}, account=account)
    if member.get('status') != 'creator' and not (member.get('status') == 'administrator' and member.get('can_post_messages')):
        raise DeliveryError('Telegram: добавьте бота администратором канала с правом публикации.')
    return {'ok': True, 'bot_username': bot.get('username', ''), 'message': 'Бот имеет право публикации в канале. Тестовое сообщение не отправлялось.'}


def send(text, media, directory, preview_mode='frame'):
    validate(text, media)
    account = credentials()
    fields = {'chat_id': account[1]}
    if not media:
        fields['text'] = text
        result = api('sendMessage', fields, account=account)
    else:
        suffix = Path(media['name']).suffix.lower()
        method, field = ('sendPhoto', 'photo') if suffix in {'.jpg', '.jpeg', '.png'} else ('sendVideo', 'video') if suffix == '.mp4' else ('sendDocument', 'document')
        fields['caption'] = text
        # Only a short extension is used in multipart headers, never an original filename.
        extension = suffix if suffix in {'.jpg', '.jpeg', '.png', '.mp4', '.pdf', '.gif', '.webp'} else '.bin'
        if method == 'sendVideo':
            with tempfile.TemporaryDirectory(prefix='vinp-video-') as temporary:
                try:
                    ready, thumbnail, metadata = video.prepare(directory / media['id'], Path(temporary))
                    if preview_mode == 'text':
                        thumbnail = video.text_cover(text, Path(temporary))
                except video.VideoError as exc:
                    raise DeliveryError('Telegram: ' + str(exc)) from None
                fields.update(metadata)
                result = api(method, fields, (field, ready, extension), account=account,
                             extra_uploads=(('thumbnail', thumbnail, '.jpg'),))
        else:
            result = api(method, fields, (field, directory / media['id'], extension), account=account)
    if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
        raise DeliveryError('Telegram: нет подтверждения доставки. Проверьте канал перед повторной публикацией.')
    return result['message_id']
