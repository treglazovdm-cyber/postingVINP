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
                with patch('telegram.video.prepare', return_value=(app.DATA / 'media' / 'test', app.DATA / 'media' / 'test', {'duration': 3, 'width': 160, 'height': 120, 'supports_streaming': 'true'})), patch('telegram.request.urlopen', return_value=response) as fetch:
                    self.assertEqual(telegram.send('Привет', media, app.DATA / 'media'), 42)
                    req = fetch.call_args.args[0]
                    self.assertTrue(req.full_url.endswith('/' + method))
                    self.assertIn('@test_channel'.encode(), req.data)
                    if field:
                        self.assertIn(f'name="{field}"'.encode(), req.data)
                        self.assertIn(b'file-bytes', req.data)
                        if method == 'sendVideo':
                            self.assertIn(b'name="thumbnail"', req.data)
                            self.assertIn(b'name="duration"\r\n\r\n3', req.data)
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

    def test_saved_settings_persist_and_never_return_token(self):
        token = '123456789:' + 'x' * 35  # Synthetic test credential.
        public = telegram.save_settings({'token': token, 'chat_id': '@first_channel'})
        self.assertTrue(public['token_saved'])
        self.assertNotIn(token, json.dumps(public))
        path = app.DATA / 'telegram-settings.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        telegram.configure_storage(app.DATA)
        self.assertEqual(telegram.credentials(), (token, '@first_channel'))
        telegram.save_settings({'token': '', 'chat_id': '@second_channel'})
        self.assertEqual(telegram.credentials(), (token, '@second_channel'))
        with patch('telegram.request.urlopen', return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":44}}')) as fetch:
            telegram.send('Hello', None, app.DATA)
            self.assertIn(token, fetch.call_args.args[0].full_url)
            self.assertEqual(json.loads(fetch.call_args.args[0].data)['chat_id'], '@second_channel')

    def test_invalid_settings_leave_existing_settings_unchanged(self):
        token = '123456789:' + 'x' * 35
        telegram.save_settings({'token': token, 'chat_id': '@test_channel'})
        for changes in ({'token':'bad','chat_id':'@test_channel'}, {'token':'','chat_id':'https://t.me/test_channel'}, {'token':123,'chat_id':'@test_channel'}):
            with self.assertRaises(ValueError):
                telegram.save_settings(changes)
            self.assertEqual(telegram.credentials(), (token, '@test_channel'))

    def test_video_text_cover_is_attached_to_send_video(self):
        path = app.DATA / 'media' / 'test'
        path.write_bytes(b'video')
        thumbnail = app.DATA / 'cover.jpg'
        thumbnail.write_bytes(b'generated-cover')
        with patch('telegram.video.prepare', return_value=(path,path,{'duration':3,'width':160,'height':120})), patch('telegram.video.text_cover', return_value=thumbnail) as generate, patch('telegram.request.urlopen', return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":99}}')) as fetch:
            self.assertEqual(telegram.send('Text cover',{'id':'test','name':'movie.mp4','size':5},app.DATA / 'media',preview_mode='text'),99)
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args[0],'Text cover')
            self.assertIn(b'generated-cover',fetch.call_args.args[0].data)
            self.assertIn(b'name="thumbnail"',fetch.call_args.args[0].data)
