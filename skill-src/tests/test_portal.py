"""Local HTTP integration tests with fake credentials and temporary files only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import campus_portal as ui

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'test.private.json'
        self.server=ui.PortalServer(('127.0.0.1',0),self.config)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def request(self,method,path,data=None,headers=None):
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        payload=json.dumps(data) if data is not None else None
        base={'Content-Type':'application/json','X-CSRF-Token':self.server.csrf_token}
        base.update(headers or {})
        conn.request(method,path,body=payload,headers=base)
        response=conn.getresponse();body=response.read();result=(response.status,dict(response.getheaders()),body);conn.close();return result
    def save(self,**changes):
        return self.request('POST','/save',{'username':'FAKE-CAMPUS-USER','password':'not-a-real-password','auto_submit':False,**changes})
    def test_initial_status_without_file(self):
        code,_,body=self.request('GET','/status');self.assertEqual(code,200);self.assertEqual(json.loads(body)['status'],'credentials_missing');self.assertFalse(self.config.exists())
    def test_page_and_assets(self):
        code,headers,body=self.request('GET','/');self.assertEqual(code,200);self.assertIn(b'csrf-token',body);self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        for name in ('app.css','app.js','mark.svg'): self.assertEqual(self.request('GET','/assets/'+name)[0],200)
    def test_save_without_echo(self):
        code,_,body=self.save();self.assertEqual(code,200);self.assertEqual(json.loads(body)['status'],'ready');self.assertNotIn(b'FAKE-CAMPUS-USER',body);self.assertNotIn(b'not-a-real-password',body)
        self.assertEqual(json.loads(self.config.read_text())['username'],'FAKE-CAMPUS-USER')
    def test_blank_preserves_credentials(self):
        self.save();code,_,_=self.save(username='',password='',auto_submit=True);self.assertEqual(code,200)
        data=json.loads(self.config.read_text());self.assertEqual(data['password'],'not-a-real-password');self.assertTrue(data['auto_submit'])
    def test_change_account_requires_new_password(self):
        self.save();self.assertEqual(self.save(username='ANOTHER-FAKE',password='')[0],400)
        self.assertEqual(json.loads(self.config.read_text())['username'],'FAKE-CAMPUS-USER')
    def test_first_config_empty_rejected(self):
        self.assertEqual(self.save(username='',password='')[0],400);self.assertFalse(self.config.exists())
    def test_missing_csrf_rejected(self):
        self.assertEqual(self.request('POST','/save',{},headers={'X-CSRF-Token':''})[0],403);self.assertFalse(self.config.exists())
    def test_external_origin_rejected(self):
        self.assertEqual(self.request('POST','/save',{},headers={'Origin':'https://evil.example'})[0],403)
    def test_host_rebinding_rejected(self):
        self.assertEqual(self.request('GET','/status',headers={'Host':'evil.example'})[0],403)
    def test_invalid_config_is_preserved(self):
        self.config.write_text('{bad JSON');self.assertEqual(self.save()[0],409);self.assertEqual(self.config.read_text(),'{bad JSON')
    def test_private_path_not_served(self):
        self.save();self.assertEqual(self.request('GET','/%E7%99%BB%E5%BD%95.private.json')[0],404)
    def test_bad_type_rejected(self):
        self.assertEqual(self.save(password=['x'])[0],400);self.assertFalse(self.config.exists())
    def test_status_does_not_return_credentials(self):
        self.save();code,_,body=self.request('GET','/status');self.assertEqual(code,200);self.assertNotIn(b'FAKE-CAMPUS-USER',body);self.assertNotIn(b'not-a-real-password',body)
    def test_shutdown_requires_header(self):
        self.assertEqual(self.request('POST','/quit')[0],403)
    def test_demo_does_not_read_or_save(self):
        self.server.demo=True
        with patch.object(ui,'read_config',side_effect=AssertionError('must not read private config')):
            self.assertEqual(self.request('GET','/status')[0],200);self.assertEqual(self.save()[0],200)
        self.assertFalse(self.config.exists())
    def test_xss_input_is_not_reflected(self):
        code,_,body=self.save(username='<img src=x onerror=alert(1)>');self.assertEqual(code,200);self.assertNotIn(b'<img',body)

if __name__=='__main__':unittest.main(verbosity=2)
