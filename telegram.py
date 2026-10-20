"""Telegram Bot API integration. Tokens never enter application responses/logs."""
import json
import os
import uuid
from pathlib import Path
from urllib import request, error


class DeliveryError(Exception):
    pass


def configured():
    return bool(os.environ.get('TELEGRAM_BOT_TOKEN', '').strip() and os.environ.get('TELEGRAM_CHAT_ID', '').strip())


def validate(text, media=None):
    # Telegram counts UTF-16 code units, including surrogate pairs for emoji.
    limit = 1024 if media else 4096
    if len(text.encode('utf-16-le')) // 2 > limit:
        raise ValueError(f'Telegram: максимум {limit} символов для текста или подписи.')
    if media and media['size'] > 49 * 1024 * 1024:
        raise ValueError('Telegram: файл должен быть не более 49 МБ.')
    if media and Path(media['name']).suffix.lower() in {'.jpg', '.jpeg', '.png'} and media['size'] > 10 * 1024 * 1024:
        raise ValueError('Telegram: фото должно быть не более 10 МБ.')


def api(method, fields, upload=None):
    if not configured():
        raise DeliveryError('Telegram: токен бота или канал не настроены.')
    if upload:
        field, path, extension = upload
        boundary = 'vinp' + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="media{extension}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        parts.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
        body, content_type = b''.join(parts), f'multipart/form-data; boundary={boundary}'
    else:
        body, content_type = json.dumps(fields).encode(), 'application/json'
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
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
    bot = api('getMe', {})
    chat = os.environ['TELEGRAM_CHAT_ID'].strip()
    member = api('getChatMember', {'chat_id': chat, 'user_id': bot['id']})
    if member.get('status') != 'creator' and not (member.get('status') == 'administrator' and member.get('can_post_messages')):
        raise DeliveryError('Telegram: добавьте бота администратором канала с правом публикации.')
    return {'ok': True, 'bot_username': bot.get('username', ''), 'message': 'Бот имеет право публикации в канале. Тестовое сообщение не отправлялось.'}


def send(text, media, directory):
    validate(text, media)
    fields = {'chat_id': os.environ['TELEGRAM_CHAT_ID'].strip()}
    if not media:
        fields['text'] = text
        result = api('sendMessage', fields)
    else:
        suffix = Path(media['name']).suffix.lower()
        method, field = ('sendPhoto', 'photo') if suffix in {'.jpg', '.jpeg', '.png'} else ('sendVideo', 'video') if suffix == '.mp4' else ('sendDocument', 'document')
        fields['caption'] = text
        # Only a short extension is used in multipart headers, never an original filename.
        extension = suffix if suffix in {'.jpg', '.jpeg', '.png', '.mp4', '.pdf', '.gif', '.webp'} else '.bin'
        result = api(method, fields, (field, directory / media['id'], extension))
    if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
        raise DeliveryError('Telegram: нет подтверждения доставки. Проверьте канал перед повторной публикацией.')
    return result['message_id']
