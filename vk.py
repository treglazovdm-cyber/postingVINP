"""VK user-token integration for community wall publishing."""
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from urllib import request, parse

_path = None
_lock = threading.RLock()
API_VERSION = '5.199'


class DeliveryError(Exception):
    pass


def configure_storage(directory):
    global _path
    _path = Path(directory) / 'vk-settings.json'


def credentials():
    with _lock:
        if _path and _path.exists():
            try:
                data = json.loads(_path.read_text())
                return str(data['token']), str(data['group_id'])
            except (OSError, ValueError, KeyError, TypeError):
                raise DeliveryError('ВК: не удалось прочитать настройки.') from None
        return '', ''


def configured():
    try:
        return all(credentials())
    except DeliveryError:
        return False


def public_settings():
    token, group = credentials()
    return {'configured': bool(token and group), 'token_saved': bool(token), 'group_id': group}


def save_settings(payload):
    token, group = payload.get('token', ''), payload.get('group_id', '')
    if not isinstance(token, str) or not isinstance(group, str):
        raise ValueError('ВК: укажите токен и числовой ID сообщества.')
    token, group = token.strip(), group.strip().lstrip('-')
    group = re.sub(r'^(club|public)', '', group)
    if not re.fullmatch(r'[1-9][0-9]{0,19}', group):
        raise ValueError('ВК: введите числовой ID сообщества, например 123456789.')
    if token and not re.fullmatch(r'[A-Za-z0-9_.-]{20,2048}', token):
        raise ValueError('ВК: проверьте токен пользователя; вставляйте только его значение.')
    with _lock:
        token = token or credentials()[0]
        if not token:
            raise ValueError('ВК: введите токен пользователя с правом wall.')
        if _path is None:
            raise OSError('Storage not initialized')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',dir=_path.parent,prefix='.vk-',delete=False) as out:
                temporary = Path(out.name)
                os.chmod(temporary,0o600)
                json.dump({'token':token,'group_id':group},out)
                out.flush(); os.fsync(out.fileno())
            os.replace(temporary,_path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return public_settings()


def api(method, fields, account=None):
    token, group = account or credentials()
    if not token or not group:
        raise DeliveryError('ВК: настройте токен пользователя и ID сообщества.')
    data = {**fields,'access_token':token,'v':API_VERSION}
    req = request.Request('https://api.vk.com/method/'+method,data=parse.urlencode(data).encode())
    try:
        with request.urlopen(req,timeout=120) as response:
            result = json.load(response)
    except (OSError,ValueError):
        raise DeliveryError('ВК: ошибка соединения. Проверьте стену сообщества перед повторной публикацией.') from None
    if not isinstance(result,dict):
        raise DeliveryError('ВК: некорректный ответ API.')
    if 'error' in result:
        code = result['error'].get('error_code')
        messages = {5:'токен недействителен или истёк.', 6:'лимит запросов. Повторная отправка автоматически не выполняется.',
                    7:'недостаточно прав токена.', 14:'требуется проверка CAPTCHA в ВК.', 15:'доступ запрещён. Проверьте роль в сообществе и права токена.',
                    17:'требуется подтверждение аккаунта в ВК.', 27:'нужен пользовательский токен, а не ключ сообщества.',
                    28:'нужен пользовательский токен, а не ключ приложения.',100:'неверные параметры запроса.', 214:'публикация на стене запрещена.',
                    222:'запрещено добавлять вложения в запись.'}
        raise DeliveryError('ВК: '+messages.get(code,'API отклонил запрос. Проверьте права и состояние сообщества.'))
    if 'response' not in result:
        raise DeliveryError('ВК: API не подтвердил выполнение запроса.')
    return result['response']


def check():
    account = credentials()
    permissions = api('account.getAppPermissions',{},account)
    if not isinstance(permissions,int) or not permissions & 8192:
        raise DeliveryError('ВК: нужен токен пользователя с правом wall. Для медиа также нужны photos и video.')
    groups = api('groups.getById',{'group_ids':account[1]},account)
    groups = groups.get('groups',[]) if isinstance(groups,dict) else groups
    if not groups or str(groups[0].get('id')) != account[1]:
        raise DeliveryError('ВК: сообщество не найдено.')
    group = groups[0]
    if not group.get('is_admin') or group.get('admin_level',0) < 2:
        raise DeliveryError('ВК: пользователь токена должен быть редактором или администратором сообщества.')
    capabilities = {'photos':bool(permissions & 4),'video':bool(permissions & 16)}
    return {'ok':True,'group_name':group.get('name',''),'capabilities':capabilities,
            'message':'Права токена и роль в сообществе проверены. Тестовая запись не публиковалась.' +
                       (' Для фото и видео выдайте также права photos и video.' if not all(capabilities.values()) else '')}


def validate(text, media=None):
    if len(text.encode('utf-16-le')) // 2 > 16384:
        raise ValueError('ВК: текст превышает 16384 символа.')
    if media and Path(media['name']).suffix.lower() not in {'.jpg','.jpeg','.png','.mp4'}:
        raise ValueError('ВК: поддерживаются фото JPG/PNG и видео MP4.')


class UploadRedirects(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        check_upload_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def check_upload_url(url):
    parsed = parse.urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443) or not any(host == suffix or host.endswith('.'+suffix) for suffix in ('vk.com','userapi.com','vkuser.net','vk-cdn.net','vkvideo.ru')):
        raise DeliveryError('ВК: API вернул неподдерживаемый адрес загрузки.')


def upload(url, field, path, extension):
    check_upload_url(url)
    boundary = 'vinp'+uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="media{extension}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
    req = request.Request(url,data=body,headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
    try:
        with request.build_opener(UploadRedirects()).open(req,timeout=180) as response:
            result = json.load(response)
    except (OSError,ValueError):
        raise DeliveryError('ВК: не удалось загрузить файл. Проверьте медиатеку сообщества перед повторной попыткой.') from None
    if not isinstance(result,dict) or 'error' in result:
        raise DeliveryError('ВК: сервер отклонил медиафайл.')
    return result


def send(text, media, directory, title='', guid=''):
    validate(text,media)
    account = credentials()
    group = account[1]
    fields = {'owner_id':'-'+group,'from_group':1,'message':text,'guid':guid}
    try:
        if media:
            suffix = Path(media['name']).suffix.lower()
            path = directory / media['id']
            if suffix == '.mp4':
                target = api('video.save',{'group_id':group,'name':(title or text)[:100],'wallpost':0},account)
                result = upload(target['upload_url'],'video_file',path,'.mp4')
                if not result.get('video_id') or str(result.get('owner_id')) != '-'+group:
                    raise DeliveryError('ВК: нет подтверждения загрузки видео в выбранное сообщество.')
                fields['attachments'] = f"video{result['owner_id']}_{result['video_id']}"
            else:
                target = api('photos.getWallUploadServer',{'group_id':group},account)
                result = upload(target['upload_url'],'photo',path,suffix)
                saved = api('photos.saveWallPhoto',{'group_id':group,'photo':result['photo'],'server':result['server'],'hash':result['hash']},account)
                photo = saved[0]
                fields['attachments'] = f"photo{photo['owner_id']}_{photo['id']}"
            # Uploaded video/photo can remain in the VK media library if wall.post fails.
        result = api('wall.post',fields,account)
        if not isinstance(result.get('post_id'),int) or result['post_id'] <= 0:
            raise DeliveryError('ВК: API не вернул ID опубликованной записи. Проверьте стену.')
        return result['post_id']
    except (KeyError,IndexError,TypeError,AttributeError):
        raise DeliveryError('ВК: неполный ответ при публикации. Проверьте стену и медиатеку перед повторной попыткой.') from None
