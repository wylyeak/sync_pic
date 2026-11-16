import unittest
from unittest.mock import patch
import app

class ScanApiTests(unittest.TestCase):
    def test_scan_post_checks_origin_and_forwards(self):
        client = app.app.test_client()
        with patch.object(app, 'runtime', return_value={'available': True, 'controller_stale': False}), patch.object(app.config_service, 'call', return_value=({'accepted': True}, 202)) as call:
            self.assertEqual(client.post('/api/scan').status_code, 403)
            self.assertEqual(client.post('/api/scan', headers={'Origin': 'https://evil.example', 'X-PicSync-Settings': '1'}).status_code, 403)
            call.assert_not_called()
            self.assertEqual(client.post('/api/scan', headers={'X-PicSync-Settings': '1'}).status_code, 202)
            call.assert_called_once_with('POST', path='/scan')

    def test_offline_controller_rejected(self):
        with patch.object(app, 'runtime', return_value={'available': True, 'controller_stale': True}):
            self.assertEqual(app.app.test_client().post('/api/scan', headers={'X-PicSync-Settings': '1'}).status_code, 503)

class ReleaseApiTests(unittest.TestCase):
    def test_origin_offline_sync_and_forwarding(self):
        client = app.app.test_client()
        run = {'available': True, 'controller_stale': False, 'syncthing': {'ready': True}}
        with patch.object(app, 'runtime', return_value=run), patch.object(app.config_service, 'call', return_value=({'accepted': True}, 202)) as call:
            url='/api/batches/test/release'
            self.assertEqual(client.post(url).status_code,403)
            self.assertEqual(client.post(url,headers={'Origin':'https://evil.example','X-PicSync-Settings':'1'}).status_code,403)
            call.assert_not_called()
            self.assertEqual(client.post(url,headers={'X-PicSync-Settings':'1'}).status_code,202)
            call.assert_called_once_with('POST',{'batch_id':'test'},path='/release')
            run['syncthing']['ready']=False
            self.assertEqual(client.post(url,headers={'X-PicSync-Settings':'1'}).status_code,409)
            run['controller_stale']=True
            self.assertEqual(client.post(url,headers={'X-PicSync-Settings':'1'}).status_code,503)

class SelectedDeleteApiTests(unittest.TestCase):
    def test_delete_requires_origin_ready_controller_and_forwards_selection(self):
        client=app.app.test_client()
        url='/api/batches/'+('a'*32)+'/remaining'
        body={'revision':'review','files':['large.mp4']}
        run={'available':True,'controller_stale':False,'syncthing':{'ready':True}}
        with patch.object(app,'runtime',return_value=run),patch.object(app.config_service,'call',return_value=({'accepted':True,'selected':1},202)) as call:
            self.assertEqual(client.post(url,json=body).status_code,403)
            self.assertEqual(client.post(url,json=body,headers={'Origin':'https://evil.example','X-PicSync-Settings':'1'}).status_code,403)
            call.assert_not_called()
            self.assertEqual(client.post(url,json=body,headers={'X-PicSync-Settings':'1'}).status_code,202)
            call.assert_called_once_with('POST',dict(body,batch_id='a'*32),path='/delete-selected')
            run['syncthing']['ready']=False
            self.assertEqual(client.post(url,json=body,headers={'X-PicSync-Settings':'1'}).status_code,409)
            run['controller_stale']=True
            self.assertEqual(client.post(url,json=body,headers={'X-PicSync-Settings':'1'}).status_code,503)

    def test_remaining_list_is_frozen_and_marks_changed_files(self):
        from unittest.mock import Mock
        import json
        cursor=Mock()
        cursor.fetchone.return_value={'phase':'manual_review','extra':json.dumps({'manual_review_revision':'review','manual_remaining':{'same.mp4':[1,2,3,4],'changed.jpg':[5,6,30,8]}})}
        cursor.fetchall.return_value=[{'source_path':'same.mp4','state':json.dumps({'member':[1,2,3,4]})},{'source_path':'changed.jpg','state':json.dumps({'member':[5,6,3,8]})}]
        context=Mock();context.__enter__=Mock(return_value=cursor);context.__exit__=Mock(return_value=False)
        with patch.object(app,'db',return_value=context):
            response=app.app.test_client().get('/api/batches/'+('a'*32)+'/remaining')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['revision'],'review')
            items={r['path']:r for r in response.json['items']}
            self.assertFalse(items['same.mp4']['changed'])
            self.assertTrue(items['changed.jpg']['changed'])
            cursor.fetchone.return_value['phase']='manual_release_sync'
            self.assertEqual(app.app.test_client().get('/api/batches/'+('a'*32)+'/remaining').status_code,409)
