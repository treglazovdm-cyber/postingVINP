"""Local content planner. Python 3.12+, no external dependencies."""
import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import telegram

ROOT = Path(__file__).parent
DATA = Path(os.environ.get('DATA_DIR', ROOT / 'data'))
PLATFORMS = {'vk', 'telegram'}
MAX_FILE = 100 * 1024 * 1024


def connect():
    db = sqlite3.connect(DATA / 'planner.sqlite', timeout=15)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'media').mkdir(exist_ok=True)
    telegram.configure_storage(DATA)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS media (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS integration_checks (
          platform TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS posts (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL,
          scheduled_at TEXT NOT NULL, platforms TEXT NOT NULL,
          media_id TEXT REFERENCES media(id), status TEXT NOT NULL,
          message TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS post_archives (
          post_id TEXT NOT NULL, platform TEXT NOT NULL, archived_at TEXT NOT NULL,
          PRIMARY KEY(post_id,platform));
        CREATE TABLE IF NOT EXISTS deliveries (
          post_id TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL,
          message TEXT NOT NULL DEFAULT '', message_id INTEGER,
          PRIMARY KEY(post_id, platform));
        ''')
        if 'preview_mode' not in [row['name'] for row in db.execute('PRAGMA table_info(posts)')]:
            db.execute("ALTER TABLE posts ADD COLUMN preview_mode TEXT NOT NULL DEFAULT 'frame'")
        if 'archived_at' not in [row['name'] for row in db.execute('PRAGMA table_info(posts)')]:
            db.execute('ALTER TABLE posts ADD COLUMN archived_at TEXT')
        # Migrate the former shared archive once, preserving existing hidden entries.
        for row in db.execute('SELECT id,platforms,archived_at FROM posts WHERE archived_at IS NOT NULL').fetchall():
            for platform in json.loads(row['platforms']):
                db.execute('INSERT OR IGNORE INTO post_archives VALUES (?,?,?)', (row['id'],platform,row['archived_at']))
        db.execute('UPDATE posts SET archived_at=NULL WHERE archived_at IS NOT NULL')


def telegram_fingerprint():
    return hashlib.sha256(json.dumps(telegram.credentials()).encode()).hexdigest()


def telegram_settings():
    settings = telegram.public_settings()
    settings.update({'connection_status': 'unconfigured' if not settings['configured'] else 'unchecked', 'checked_at': None})
    with connect() as db:
        row = db.execute("SELECT * FROM integration_checks WHERE platform='telegram'").fetchone()
    if settings['configured'] and row and row['fingerprint'] == telegram_fingerprint():
        settings['checked_at'] = row['checked_at']
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds()
        settings['connection_status'] = row['status'] if age < 86400 else 'stale'
    return settings


def record_telegram_check(fingerprint, status):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO integration_checks VALUES ('telegram',?,?,?)",
                   (fingerprint, status, datetime.now(timezone.utc).isoformat()))


def create_post(payload):
    preview_mode = payload.get('preview_mode', 'frame')
    if preview_mode not in ('frame', 'text'):
        raise ValueError('Неверный тип обложки.')
    title = str(payload.get('title', '')).strip()
    body = str(payload.get('text', '')).strip()
    platforms = payload.get('platforms', [])
    if not title or len(title) > 200 or len(body) > 10000:
        raise ValueError('Укажите название до 200 символов и текст до 10000 символов.')
    if not isinstance(platforms, list) or not platforms or any(p not in PLATFORMS for p in platforms):
        raise ValueError('Выберите хотя бы одну поддерживаемую площадку.')
    try:
        date = datetime.fromisoformat(payload.get('scheduled_at', '').replace('Z', '+00:00'))
        if date.tzinfo is None or date <= datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Выберите будущее время с часовым поясом.') from None
    media_id = payload.get('media_id') or None
    with connect() as db:
        if media_id and not db.execute('SELECT 1 FROM media WHERE id=?', (media_id,)).fetchone():
            raise ValueError('Файл не найден.')
        if 'telegram' in platforms:
            media = db.execute('SELECT * FROM media WHERE id=?', (media_id,)).fetchone() if media_id else None
            telegram.validate(body or title, dict(media) if media else None)
        post_id = uuid.uuid4().hex
        db.execute('INSERT INTO posts (id,title,text,scheduled_at,platforms,media_id,status,message,preview_mode) VALUES (?,?,?,?,?,?,?,?,?)',
                   (post_id, title, body, date.astimezone(timezone.utc).isoformat(),
                    json.dumps(platforms), media_id, 'scheduled', '', preview_mode))
    return post_id


def process_due(now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    with connect() as db:
        candidates = [dict(r) for r in db.execute("SELECT * FROM posts WHERE status='scheduled' AND scheduled_at<=?", (now,))]
    for post in candidates:
        with connect() as db:
            claimed = db.execute("UPDATE posts SET status='sending' WHERE id=? AND status='scheduled'", (post['id'],)).rowcount
        if not claimed:
            continue
        results = []
        for platform in json.loads(post['platforms']):
            with connect() as db:
                db.execute("INSERT OR IGNORE INTO deliveries(post_id,platform,status) VALUES (?,?,'sending')", (post['id'], platform))
            state, message, message_id = 'blocked', 'ВК: интеграция ещё не подключена.', None
            if platform == 'telegram':
                if not telegram.configured():
                    message = 'Telegram: публикация не отправлена — задайте токен бота и канал в настройках интеграции.'
                else:
                    try:
                        with connect() as db:
                            media = db.execute('SELECT * FROM media WHERE id=?', (post['media_id'],)).fetchone()
                        options = {'preview_mode': 'text'} if post.get('preview_mode') == 'text' else {}
                        message_id = telegram.send(post['text'] or post['title'], dict(media) if media else None, DATA / 'media', **options)
                        state, message = 'sent', 'Telegram: отправлено.'
                    except telegram.DeliveryError as error:
                        state, message = 'failed', str(error)
                    except Exception:
                        state, message = 'failed', 'Telegram: неожиданный сбой. Проверьте канал перед повторной публикацией.'
            with connect() as db:
                db.execute('UPDATE deliveries SET status=?,message=?,message_id=? WHERE post_id=? AND platform=?',
                           (state, message, message_id, post['id'], platform))
            results.append((state, message))
        states = [r[0] for r in results]
        final = 'sent' if all(s == 'sent' for s in states) else 'partial' if 'sent' in states else 'failed' if 'failed' in states else 'blocked'
        with connect() as db:
            db.execute('UPDATE posts SET status=?,message=? WHERE id=?', (final, ' '.join(r[1] for r in results), post['id']))


def worker():
    while True:
        try:
            process_due()
        except sqlite3.Error:
            print('Scheduler database error', flush=True)
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':
            with connect() as db:
                db.execute('SELECT 1 FROM posts LIMIT 1')
            return self.reply(200, {'status': 'ok', 'delivery_enabled': telegram.configured()})
        if path == '/api/telegram':
            try:
                return self.reply(200, telegram_settings())
            except telegram.DeliveryError as exc:
                return self.reply(500, {'error': str(exc)})
        if path == '/api/posts':
            with connect() as db:
                posts = [dict(r) for r in db.execute('SELECT posts.*, media.name AS media_name FROM posts LEFT JOIN media ON media.id=posts.media_id ORDER BY scheduled_at')]
            for post in posts:
                post['platforms'] = json.loads(post['platforms'])
                with connect() as db:
                    post['deliveries'] = [dict(r) for r in db.execute('SELECT platform,status,message,message_id FROM deliveries WHERE post_id=?', (post['id'],))]
                    archives = [dict(r) for r in db.execute('SELECT platform,archived_at FROM post_archives WHERE post_id=?', (post['id'],))]
                    post['archives'] = [r['platform'] for r in archives]
                    post['archived_at'] = next((r['archived_at'] for r in archives if r['platform']=='telegram'), None)
            return self.reply(200, posts)
        if path == '/api/media':
            with connect() as db:
                return self.reply(200, [dict(r) for r in db.execute('SELECT * FROM media ORDER BY rowid DESC')])
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path in assets:
            name, mime = assets[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), mime)
        self.reply(404, {'error': 'Не найдено'})

    def do_POST(self):
        # Require same-origin browser requests; do not accept cross-site writes.
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.reply(403, {'error': 'Недопустимый источник запроса'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > MAX_FILE:
                return self.reply(413, {'error': 'Размер запроса должен быть от 1 байта до 100 МБ.'})
            path = urlsplit(self.path).path
            if path == '/api/media':
                from urllib.parse import unquote
                name = Path(unquote(self.headers.get('X-Filename', 'file'))).name[:200]
                media_id = uuid.uuid4().hex
                target = DATA / 'media' / media_id
                remaining = size
                try:
                    with target.open('xb') as out:
                        while remaining:
                            chunk = self.rfile.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise ValueError('Загрузка прервана.')
                            out.write(chunk)
                            remaining -= len(chunk)
                    with connect() as db:
                        db.execute('INSERT INTO media VALUES (?,?,?)', (media_id, name, size))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                return self.reply(201, {'id': media_id, 'name': name})
            if size > 64000:
                return self.reply(413, {'error': 'Слишком большой запрос'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('Ожидается объект JSON.')
            if path == '/api/preview':
                import tempfile
                import video
                text = payload.get('text', '')
                if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                    return self.reply(400, {'error': 'Введите текст поста или название для обложки.'})
                try:
                    with tempfile.TemporaryDirectory(prefix='vinp-cover-') as temporary:
                        cover = video.text_cover(text, Path(temporary))
                        return self.reply(200, cover.read_bytes(), 'image/jpeg')
                except video.VideoError as exc:
                    return self.reply(400, {'error': str(exc)})
            if path == '/api/telegram/settings':
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    return self.reply(415, {'error': 'Ожидается application/json.'})
                try:
                    return self.reply(200, telegram.save_settings(payload))
                except OSError:
                    return self.reply(500, {'error': 'Не удалось сохранить настройки. Проверьте доступ приложения к каталогу данных.'})
                except telegram.DeliveryError as exc:
                    return self.reply(500, {'error': str(exc)})
            if path == '/api/telegram/check':
                fingerprint = None
                try:
                    fingerprint = telegram_fingerprint()
                    result = telegram.check()
                    record_telegram_check(fingerprint, 'verified')
                    return self.reply(200, result)
                except telegram.DeliveryError as error:
                    if fingerprint:
                        record_telegram_check(fingerprint, 'error')
                    return self.reply(400, {'error': str(error)})
            if path == '/api/posts':
                return self.reply(201, {'id': create_post(payload)})
            if path.startswith('/api/archive/') or path.startswith('/api/unarchive/'):
                post_id = path.rsplit('/', 1)[-1]
                archive = path.startswith('/api/archive/')
                platform = payload.get('platform', 'telegram')
                if platform not in PLATFORMS:
                    return self.reply(400, {'error': 'Неизвестная площадка.'})
                with connect() as db:
                    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
                    if not post:
                        return self.reply(404, {'error': 'Запись не найдена.'})
                    if platform not in json.loads(post['platforms']):
                        return self.reply(409, {'error': 'Эта запись не относится к выбранной площадке.'})
                    if archive:
                        published = db.execute("SELECT 1 FROM deliveries WHERE post_id=? AND platform=? AND status='sent' AND message_id IS NOT NULL", (post_id,platform)).fetchone()
                        if (not published and post['status'] != 'cancelled') or post['status'] == 'sending':
                            return self.reply(409, {'error': 'В архив можно перенести опубликованную или отменённую запись.'})
                        db.execute('INSERT OR IGNORE INTO post_archives VALUES (?,?,?)', (post_id,platform,datetime.now(timezone.utc).isoformat()))
                    else:
                        db.execute('DELETE FROM post_archives WHERE post_id=? AND platform=?', (post_id,platform))
                return self.reply(200, {'archived': archive, 'platform': platform})
            if path.startswith('/api/cancel/'):
                with connect() as db:
                    result = db.execute("UPDATE posts SET status='cancelled', message='' WHERE id=? AND status IN ('scheduled','blocked')", (path.rsplit('/', 1)[-1],))
                return self.reply(200 if result.rowcount else 404, {'updated': bool(result.rowcount)})
            self.reply(404, {'error': 'Не найдено'})
        except ValueError as exc:
            message = str(exc) if str(exc).startswith('Telegram:') else 'Проверьте поля, дату публикации и выбранный файл.'
            self.reply(400, {'error': message})
        except (TypeError, AttributeError):
            self.reply(400, {'error': 'Проверьте поля, дату публикации и выбранный файл.'})


if __name__ == '__main__':
    initialize()
    # A restart during a request has an unknown outcome; never blindly resend.
    with connect() as db:
        db.execute("UPDATE posts SET status='failed', message='Отправка прервана перезапуском. Проверьте канал: сообщение могло быть доставлено.' WHERE status='sending'")
        db.execute("UPDATE deliveries SET status='failed', message='Результат отправки неизвестен после перезапуска.' WHERE status='sending'")
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer((os.environ.get('HOST', '127.0.0.1'), int(os.environ.get('PORT', '8000'))), Handler)
    print('Content planner started', flush=True)
    server.serve_forever()
