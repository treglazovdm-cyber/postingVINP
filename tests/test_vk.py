import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import parse, error

import app
import vk


class VkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original = app.DATA
        app.DATA = Path(self.directory.name)
        app.initialize()
        self.token = 'vk1.a.'+'x'*50
        vk.save_settings({'token':self.token,'group_id':'club12345'})

    def tearDown(self):
        app.DATA = self.original
        self.directory.cleanup()

    def test_settings_preserve_token_and_redact_public_response(self):
        result = vk.save_settings({'token':'','group_id':'-67890'})
        self.assertEqual(vk.credentials(),(self.token,'67890'))
        self.assertNotIn(self.token,json.dumps(result))
        self.assertEqual((app.DATA / 'vk-settings.json').stat().st_mode & 0o777,0o600)
        for data in ({'token':'bad token','group_id':'12345'},{'token':'','group_id':'https://vk.com/example'}):
            with self.assertRaises(ValueError): vk.save_settings(data)
        self.assertEqual(vk.credentials(),(self.token,'67890'))

    def test_api_uses_token_in_post_body_and_sanitizes_errors(self):
        with patch('vk.request.urlopen',return_value=io.BytesIO(b'{"response":{"post_id":42}}')) as fetch:
            self.assertEqual(vk.api('wall.post',{'owner_id':-12345})['post_id'],42)
            req=fetch.call_args.args[0]
            self.assertNotIn(self.token,req.full_url)
            self.assertEqual(parse.parse_qs(req.data.decode())['access_token'],[self.token])
        with patch('vk.request.urlopen',return_value=io.BytesIO(json.dumps({'error':{'error_code':27,'error_msg':self.token}}).encode())):
            with self.assertRaises(vk.DeliveryError) as result: vk.api('wall.post',{})
            self.assertNotIn(self.token,str(result.exception))
            self.assertIn('пользовательский',str(result.exception))
        with patch('vk.request.urlopen',side_effect=error.URLError(self.token)):
            with self.assertRaises(vk.DeliveryError) as result: vk.api('wall.post',{})
            self.assertNotIn(self.token,str(result.exception))

    def test_check_permissions_and_editor_role_without_posting(self):
        with patch('vk.api',side_effect=[8192|4|16,{'groups':[{'id':12345,'is_admin':1,'admin_level':2,'name':'Тест'}]}]) as call:
            result=vk.check()
            self.assertTrue(result['ok'])
            self.assertTrue(all(result['capabilities'].values()))
            self.assertEqual([c.args[0] for c in call.call_args_list],['account.getAppPermissions','groups.getById'])
        with patch('vk.api',return_value=4):
            with self.assertRaises(vk.DeliveryError): vk.check()
        with patch('vk.api',side_effect=[8192,[{'id':12345,'is_admin':1,'admin_level':1}]]):
            with self.assertRaises(vk.DeliveryError): vk.check()

    def test_text_publish_has_group_owner_and_stable_guid(self):
        with patch('vk.api',return_value={'post_id':77}) as call:
            self.assertEqual(vk.send('Текст',None,app.DATA,guid='stable-id'),77)
            self.assertEqual(call.call_args.args[0],'wall.post')
            fields=call.call_args.args[1]
            self.assertEqual(fields['owner_id'],'-12345')
            self.assertEqual(fields['from_group'],1)
            self.assertEqual(fields['guid'],'stable-id')

    def test_photo_upload_save_and_wall_attachment(self):
        path=app.DATA/'media'/'sample';path.write_bytes(b'photo')
        with patch('vk.api',side_effect=[{'upload_url':'https://pu.vk.com/upload'},[{'owner_id':-12345,'id':9}],{'post_id':80}]) as call,patch('vk.upload',return_value={'server':1,'photo':'photo-data','hash':'upload-hash'}) as upload:
            self.assertEqual(vk.send('Фото',{'id':'sample','name':'a.jpg'},app.DATA/'media'),80)
            self.assertEqual([c.args[0] for c in call.call_args_list],['photos.getWallUploadServer','photos.saveWallPhoto','wall.post'])
            self.assertEqual(call.call_args.args[1]['attachments'],'photo-12345_9')
            self.assertEqual(upload.call_args.args[1],'photo')

    def test_video_upload_does_not_post_until_wall_call(self):
        path=app.DATA/'media'/'sample';path.write_bytes(b'video')
        with patch('vk.api',side_effect=[{'upload_url':'https://pu.vk.com/upload'},{'post_id':81}]) as call,patch('vk.upload',return_value={'owner_id':-12345,'video_id':10}):
            self.assertEqual(vk.send('Видео',{'id':'sample','name':'a.mp4'},app.DATA/'media',title='Ролик'),81)
            self.assertEqual(call.call_args_list[0].args[1]['wallpost'],0)
            self.assertEqual(call.call_args.args[1]['attachments'],'video-12345_10')

    def test_upload_validates_destination_and_preserves_file_bytes(self):
        for url in ('http://pu.vk.com/u','https://evil.example/u','https://vk.com.evil.example/u','https://u:p@pu.vk.com/u'):
            with self.assertRaises(vk.DeliveryError): vk.check_upload_url(url)
        path=app.DATA/'sample';path.write_bytes(b'file-bytes')
        with patch('vk.request.build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(b'{"photo":"x","server":1,"hash":"x"}')
            vk.upload('https://pu.vk.com/u','photo',path,'.jpg')
            req=opener.return_value.open.call_args.args[0]
            self.assertIn(b'name="photo"',req.data)
            self.assertIn(b'file-bytes',req.data)
            self.assertNotIn(self.token.encode(),req.data)

    def test_scheduler_tracks_independent_results_and_never_reposts(self):
        post_id=app.create_post({'title':'Тест','text':'Текст','platforms':['vk','telegram'],'scheduled_at':'2030-01-01T12:00:00+00:00'})
        with patch('telegram.configured',return_value=False),patch('vk.send',return_value=95) as send:
            app.process_due('2035-01-01T12:00:00+00:00');app.process_due('2035-01-01T12:00:00+00:00')
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs['guid'],post_id)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM posts WHERE id=?',(post_id,)).fetchone()[0],'partial')
            self.assertEqual(db.execute("SELECT message_id FROM deliveries WHERE platform='vk'").fetchone()[0],95)

    def test_vk_badge_persists_and_resets_for_different_group(self):
        app.record_vk_check(app.vk_fingerprint(),'verified')
        app.initialize()
        self.assertEqual(app.vk_settings()['connection_status'],'verified')
        vk.save_settings({'token':'','group_id':'67890'})
        self.assertEqual(app.vk_settings()['connection_status'],'unchecked')
