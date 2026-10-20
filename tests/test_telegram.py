import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import app
import telegram


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = app.DATA
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.env = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '@test_channel'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        app.DATA = self.old
        self.temp.cleanup()

    def test_text_photo_video_document_requests(self):
        for filename, method, field in [('image.jpg', 'sendPhoto', 'photo'), ('movie.mp4', 'sendVideo', 'video'), ('a.pdf', 'sendDocument', 'document'), (None, 'sendMessage', None)]:
            with self.subTest(filename=filename):
                media = None
                if filename:
                    (app.DATA / 'media' / 'test').write_bytes(b'file-bytes')
                    media = {'id': 'test', 'name': filename, 'size': 10}
                response = io.BytesIO(json.dumps({'ok': True, 'result': {'message_id': 42}}).encode())
                with patch('telegram.request.urlopen', return_value=response) as fetch:
                    self.assertEqual(telegram.send('Привет', media, app.DATA / 'media'), 42)
                    req = fetch.call_args.args[0]
                    self.assertTrue(req.full_url.endswith('/' + method))
                    self.assertIn('@test_channel'.encode(), req.data)
                    if field:
                        self.assertIn(f'name="{field}"'.encode(), req.data)
                        self.assertIn(b'file-bytes', req.data)
                    else:
                        self.assertEqual(json.loads(req.data)['text'], 'Привет')

    def test_api_failures_do_not_expose_token(self):
        for failure in [HTTPError('https://api.telegram.org/bottest-token/sendMessage', 403, 'secret', {}, None), URLError('test-token')]:
            with patch('telegram.request.urlopen', side_effect=failure):
                with self.assertRaises(telegram.DeliveryError) as result:
                    telegram.send('Test', None, app.DATA)
                self.assertNotIn('test-token', str(result.exception))

    def test_mixed_platform_result_and_no_duplicate_send(self):
        post_id = app.create_post({'title': 'Test', 'text': 'Hello', 'platforms': ['telegram', 'vk'], 'scheduled_at': '2030-01-01T12:00:00+00:00'})
        with patch('telegram.send', return_value=55) as send:
            app.process_due('2031-01-01T12:00:00+00:00')
            app.process_due('2031-01-01T12:00:00+00:00')
            send.assert_called_once()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM posts WHERE id=?', (post_id,)).fetchone()[0], 'partial')
            self.assertEqual(db.execute("SELECT message_id FROM deliveries WHERE platform='telegram'").fetchone()[0], 55)

    def test_uncertain_delivery_not_automatically_retried(self):
        app.create_post({'title': 'Test', 'text': 'Hello', 'platforms': ['telegram'], 'scheduled_at': '2030-01-01T12:00:00+00:00'})
        with patch('telegram.send', side_effect=telegram.DeliveryError('Результат неизвестен')) as send:
            app.process_due('2031-01-01T12:00:00+00:00')
            app.process_due('2031-01-01T12:00:00+00:00')
            send.assert_called_once()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM posts').fetchone()[0], 'failed')

    def test_limits_and_permissions(self):
        with self.assertRaises(ValueError):
            telegram.validate('a' * 1025, {'name': 'x.mp4', 'size': 1})
        with self.assertRaises(ValueError):
            telegram.validate('😀' * 2049)
        with self.assertRaises(ValueError):
            telegram.validate('a', {'name': 'x.jpg', 'size': 11 * 1024 * 1024})
        with patch('telegram.api', side_effect=[{'id': 1}, {'status': 'administrator', 'can_post_messages': True}]):
            self.assertTrue(telegram.check()['ok'])
        with patch('telegram.api', side_effect=[{'id': 1}, {'status': 'member'}]):
            with self.assertRaises(telegram.DeliveryError):
                telegram.check()
