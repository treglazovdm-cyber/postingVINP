import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = app.DATA
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DATA = self.previous
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        conn.request(method, path, body, headers or {})
        result = conn.getresponse()
        status, data = result.status, result.read()
        conn.close()
        return status, data

    def payload(self, **changes):
        data = dict(title='Тест', text='Новая запись', platforms=['vk', 'telegram'],
                    scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        data.update(changes)
        return data

    def test_http_create_upload_read_cancel_and_persist(self):
        status, data = self.request('POST', '/api/media', b'example-content', {'X-Filename': '%D1%82%D0%B5%D1%81%D1%82.mp4'})
        self.assertEqual(status, 201)
        media_id = json.loads(data)['id']
        self.assertEqual((app.DATA / 'media' / media_id).read_bytes(), b'example-content')
        status, data = self.request('POST', '/api/posts', json.dumps(self.payload(media_id=media_id)).encode())
        self.assertEqual(status, 201)
        post_id = json.loads(data)['id']
        app.initialize()  # Startup is repeatable and doesn't clear saved data.
        status, data = self.request('GET', '/api/posts')
        self.assertEqual(status, 200)
        post = json.loads(data)[0]
        self.assertEqual(post['media_name'], 'тест.mp4')
        self.assertEqual(post['platforms'], ['vk', 'telegram'])
        self.assertEqual(post['status'], 'scheduled')
        status, _ = self.request('POST', '/api/cancel/' + post_id, '{}')
        self.assertEqual(status, 200)
        app.process_due((datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.assertEqual(json.loads(self.request('GET', '/api/posts')[1])[0]['status'], 'cancelled')

    def test_due_records_block_without_false_delivery(self):
        first = app.create_post(self.payload())
        second = app.create_post(self.payload(scheduled_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat()))
        app.process_due((datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
        with app.connect() as db:
            rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM posts')}
        self.assertEqual(rows[first]['status'], 'blocked')
        self.assertIn('не отправлена', rows[first]['message'])
        self.assertEqual(rows[second]['status'], 'scheduled')

    def test_invalid_posts_and_cross_origin_are_rejected(self):
        for changes in ({'platforms': []}, {'platforms': ['unknown']},
                        {'scheduled_at': '2020-01-01T00:00:00Z'},
                        {'scheduled_at': '2030-01-01T00:00:00'},
                        {'media_id': 'missing'}, {'platforms': ['youtube']}, {'title': ''}):
            with self.subTest(changes=changes):
                status, _ = self.request('POST', '/api/posts', json.dumps(self.payload(**changes)).encode())
                self.assertEqual(status, 400)
        status, _ = self.request('POST', '/api/posts', '{}', {'Origin': 'https://other.example'})
        self.assertEqual(status, 403)
        status, _ = self.request('POST', '/api/posts', '[]')
        self.assertEqual(status, 400)

    def test_health_ui_and_path_traversal(self):
        status, body = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['delivery_enabled'])
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Контент-план'.encode(), body)
        self.assertEqual(self.request('GET', '/../../app.py')[0], 404)

    def test_settings_api_redacts_token_and_rejects_cross_origin(self):
        token = '123456789:' + 'z' * 35
        payload = json.dumps({'token': token, 'chat_id': '@test_channel'})
        status, body = self.request('POST', '/api/telegram/settings', payload, {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertNotIn(token.encode(), body)
        status, body = self.request('GET', '/api/telegram')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['chat_id'], '@test_channel')
        self.assertNotIn(token.encode(), body)
        status, _ = self.request('POST', '/api/telegram/settings', payload, {'Origin': 'https://other.example', 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        status, _ = self.request('POST', '/api/telegram/settings', payload, {'Content-Type': 'text/plain'})
        self.assertEqual(status, 415)

    def test_cover_endpoint_and_saved_selection(self):
        status, image = self.request('POST','/api/preview',json.dumps({'text':'Упражнения для пресса'}).encode())
        self.assertEqual(status, 200)
        self.assertTrue(image.startswith(b'\xff\xd8'))
        status, _ = self.request('POST','/api/preview','{"text":""}')
        self.assertEqual(status, 400)
        post_id = app.create_post(self.payload(preview_mode='text'))
        app.initialize()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT preview_mode FROM posts WHERE id=?',(post_id,)).fetchone()[0], 'text')

    def test_archive_and_restore_preserve_telegram_post_without_api_calls(self):
        post_id = app.create_post(self.payload())
        with app.connect() as db:
            db.execute("UPDATE posts SET status='sent',message='Telegram: отправлено.' WHERE id=?",(post_id,))
            db.execute("INSERT INTO deliveries(post_id,platform,status,message_id) VALUES (?,'telegram','sent',42)",(post_id,))
        with patch('telegram.api') as remote:
            for _ in range(2):
                self.assertEqual(self.request('POST','/api/archive/'+post_id,'{}')[0],200)
            app.initialize()
            post = json.loads(self.request('GET','/api/posts')[1])[0]
            self.assertTrue(post['archived_at'])
            self.assertEqual(post['status'],'sent')
            self.assertEqual(post['deliveries'][0]['message_id'],42)
            self.assertEqual(post['deliveries'][0]['status'],'sent')
            self.assertEqual(self.request('POST','/api/unarchive/'+post_id,'{}')[0],200)
            app.process_due('2035-01-01T00:00:00+00:00')
            remote.assert_not_called()
        post = json.loads(self.request('GET','/api/posts')[1])[0]
        self.assertIsNone(post['archived_at'])
        self.assertEqual(post['status'],'sent')

    def test_unsent_post_cannot_be_archived(self):
        post_id = app.create_post(self.payload())
        self.assertEqual(self.request('POST','/api/archive/'+post_id,'{}')[0],409)
        self.assertEqual(self.request('POST','/api/archive/missing','{}')[0],404)

    def test_cancelled_post_can_be_archived_and_restored_without_publication(self):
        post_id = app.create_post(self.payload())
        self.assertEqual(self.request('POST','/api/cancel/'+post_id,'{}')[0],200)
        with patch('telegram.api') as remote:
            self.assertEqual(self.request('POST','/api/archive/'+post_id,'{}')[0],200)
            app.initialize()
            post = json.loads(self.request('GET','/api/posts')[1])[0]
            self.assertEqual(post['status'],'cancelled')
            self.assertTrue(post['archived_at'])
            self.assertEqual(self.request('POST','/api/unarchive/'+post_id,'{}')[0],200)
            app.process_due('2035-01-01T00:00:00+00:00')
            remote.assert_not_called()
        post = json.loads(self.request('GET','/api/posts')[1])[0]
        self.assertEqual(post['status'],'cancelled')
        self.assertIsNone(post['archived_at'])


if __name__ == '__main__':
    unittest.main()
