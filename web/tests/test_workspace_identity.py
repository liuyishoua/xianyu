import tempfile
import unittest
from pathlib import Path
from werkzeug.test import Client
from werkzeug.wrappers import Response
from web.app import create_app
from web.demo import DemoPlatform
from web.workspace_identity import WorkspaceIdentity

class TenantTests(unittest.TestCase):
 def test_platform_accounts_are_isolated_even_when_headers_are_forged(self):
  with tempfile.TemporaryDirectory() as tmp:
   apps=[]
   def factory(path):
    app=create_app(path,platform=DemoPlatform('tenant-'+Path(path).name,'synthetic'),auth=False);apps.append(app);return app
   root=factory(Path(tmp)/'owner');gate=WorkspaceIdentity(root,Path(tmp)/'owner','xianyu',factory,1,verify=lambda t:{'id':int(t),'username':'synthetic','app_access':{'xianyuapis':True}} if t in ('1','2') else None);root.wsgi_app=gate;c=Client(root,Response)
   c.set_cookie('ops_workspace_session','1');first=c.get('/api/accounts').json
   c.set_cookie('ops_workspace_session','2');second=c.get('/api/accounts').json
   self.assertNotEqual(first,second)
   response=c.get('/api/products',headers={'X-Account-ID':'tenant-owner'});self.assertEqual(response.status_code,404)
   for app in apps:app.extensions['workspace'].executor.shutdown(wait=True)
