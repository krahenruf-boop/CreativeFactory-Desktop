import json,subprocess
from pathlib import Path
class Keychain:
 def __init__(self,helper):self.helper=str(helper)
 def call(self,operation,account,value=None):
  req=dict(operation=operation,account=account)
  if value is not None:req['value']=value
  p=subprocess.run([self.helper],input=json.dumps(req),capture_output=True,text=True,timeout=45)
  if p.returncode:raise ValueError('KEYCHAIN_UNAVAILABLE')
  return json.loads(p.stdout)
 def get(self,name):return self.call('get',name).get('value','')
 def configured(self,name):return self.call('status',name).get('configured',False)
 def set(self,name,value):return self.call('set',name,value)
 def delete(self,name):return self.call('delete',name)
