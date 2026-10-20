"""Read-only Feishu Sheets adapter. No browser credentials or provider calls.

Bindings and atomic snapshots live exclusively in AutoCut user data. Only the
selected sheet/row enters the existing reference and language approval flow.
"""
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from .script_language import detect_script_language

ERRORS = {
 'FEISHU_URL_INVALID': '请填写有效的飞书知识库或电子表格 HTTPS 链接',
 'FEISHU_AUTH_REQUIRED': '请先配置飞书自建应用 App ID 和 App Secret，并开通只读权限',
 'FEISHU_AUTH_FAILED': '飞书应用凭据无效或已过期，请检查 App ID 和 App Secret',
 'FEISHU_PERMISSION_REQUIRED': '飞书拒绝读取：请发布应用的只读权限，并将文档授权给该应用',
 'FEISHU_UNSUPPORTED_TYPE': '此链接不是飞书电子表格，目前仅支持 Sheets',
 'FEISHU_NETWORK_ERROR': '飞书连接失败，请检查网络后重试；已有缓存保持不变',
 'FEISHU_API_ERROR': '飞书返回异常，未更新缓存，请稍后重试',
 'FEISHU_RATE_LIMIT': '飞书请求频率受限，请稍后重试；已有缓存保持不变',
 'FEISHU_NOT_BOUND': '请先在设置中绑定飞书文案库',
 'FEISHU_NOT_SYNCED': '文案库还没有同步成功，请先立即同步',
 'FEISHU_SHEET_NOT_FOUND': '未找到指定工作表，请检查链接或刷新文案库',
 'FEISHU_HEADERS_UNKNOWN': '未识别到文案字段，请检查工作表实际表头',
 'FEISHU_MARKET_MISMATCH': '工作表所属市场与当前任务不一致，请重新选择',
 'FEISHU_SYNC_BUSY': '文案库正在同步，请等待本次完成',
 'FEISHU_TOO_LARGE': '表格超出本次安全读取范围，未保存不完整数据',
}

def digest(value):
 return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def parse_link(url):
 try:
  p = urlparse(str(url).strip())
  if p.scheme != 'https' or not (p.hostname or '').endswith('.feishu.cn') or p.username or p.password or p.port not in (None, 443): raise ValueError()
  match = re.fullmatch(r'/(wiki|sheets)/([A-Za-z0-9]+)/*', p.path)
  if not match: raise ValueError()
  kind, token = match.groups(); sheet = parse_qs(p.query).get('sheet', [''])[0]
  if sheet and not re.fullmatch(r'[A-Za-z0-9]+', sheet): raise ValueError()
  return dict(url=str(url).strip(), link_type=kind, node_token=token if kind == 'wiki' else None,
              spreadsheet_token=token if kind == 'sheets' else None, selected_sheet_id=sheet,
              source_id=digest([kind, token])[:24])
 except (ValueError, TypeError): raise ValueError('FEISHU_URL_INVALID') from None

def sheet_product(name):
 match = re.match(r'^(BR|MX)[_＿\s-]+(.+)$', name.strip(), re.I)
 if not match: return dict(market=None, target_language=None, product_name=name)
 code, product = match.groups()
 return dict(market='Brazil' if code.upper() == 'BR' else 'Mexico',
             target_language='pt-BR' if code.upper() == 'BR' else 'es-MX', product_name=product.strip())

def cell(value):
 if value is None: return ''
 if isinstance(value, list): return ''.join(cell(v) for v in value)
 if isinstance(value, dict): return cell(value.get('text', value.get('value', '')))
 return str(value).strip()

ALIASES = {
 'text': {'文案', '文案内容', '口播', '口播文案', '脚本', 'script', 'content', 'target_text', '目标语言文案', 'pt-br', 'es-mx', '葡语文案', '西语文案', '英文文案', '印尼文案', 'en'},
 'zh': {'中文文案', '中文', '中文对照', 'chinese_text', 'text_zh', 'zh-cn'},
 'id': {'文案编号', '编号', 'script_id', 'id'},
 'type': {'类型', '文案类型', 'copy_type'},
 'category': {'类', '类别', '内容类别'},
 'country': {'国家', '市场', '语言', 'country', 'market', 'language'},
 'title': {'标题', '标题文案', 'title'},
 'performance': {'出单数', '出单', '出单数据', '订单数', 'performance', 'orders'},
 'notes': {'备注', 'notes'},
}

def normalize(source_id, sheet, values, synced):
 """Recognize headers, never fixed columns. Preserve raw cells and conflicts."""
 header_index = None; mapping = {}; headers = []; structure_table = False
 for i, values_row in enumerate(values[:30]):
  names = [cell(v) for v in values_row]
  found = {k: next((n for n, h in enumerate(names) if h.casefold() in aliases), None) for k, aliases in ALIASES.items()}
  if '常规结构' in names and '专属结构' in names:
   found['category']=names.index('常规结构');found['text']=names.index('专属结构');structure_table=True
  # A data row beginning with the category "文案" is not a header.
  plausible = not any(len(h)>80 for h in names) and (sum(v is not None for v in found.values())>=2 or len([n for n in names if n])==1)
  if plausible and (found['text'] is not None or found['zh'] is not None):
   header_index = i; headers = names; mapping = found; break
 info = sheet_product(sheet['title']); result = dict(sheet_id=sheet['sheet_id'], sheet_name=sheet['title'], **info,
      headers=headers, header_row=header_index + 1 if header_index is not None else None, rows=[], warnings=[])
 if header_index is None:
  if any(cell(v)=='即梦指令' for row in values[:10] for v in row):result['warnings']=['NON_COPY_SHEET']
  else:result['warnings'] = ['FEISHU_HEADERS_UNKNOWN']
  return result
 for i, values_row in enumerate(values[header_index+1:], header_index+2):
  raw = {h: cell(values_row[n]) if n < len(values_row) else '' for n, h in enumerate(headers) if h}
  def get(key):
   n = mapping[key]; return cell(values_row[n]) if n is not None and n < len(values_row) else ''
  category = get('category'); text = get('text'); zh = get('zh')
  if structure_table and category!='文案':continue
  if category and category not in {'文案', '口播', '脚本', 'script', 'copy'}: continue
  if not text and not zh: continue
  if text.casefold() in ALIASES['text'] or zh.casefold() in ALIASES['zh']: continue
  if not re.search(r'[A-Za-z\u3400-\u9fff]', text or zh): continue
  original = text or zh; language = detect_script_language(original); warnings = []
  if language=='unknown' and mapping['text'] is not None and headers[mapping['text']]=='印尼文案':language='id'
  if language=='zh-CN' and not zh:zh=original
  declared = get('country')
  # Never trust a stale country cell to relabel a foreign-language script.
  if declared and language == 'es-MX' and re.search(r'巴西|葡|Brazil|pt', declared, re.I): warnings.append('SOURCE_COUNTRY_LANGUAGE_CONFLICT')
  if declared and language == 'pt-BR' and re.search(r'墨西哥|西班牙|Mexico|es-MX', declared, re.I): warnings.append('SOURCE_COUNTRY_LANGUAGE_CONFLICT')
  if info['target_language'] and language not in {info['target_language'], 'unknown', 'zh-CN'}: warnings.append('NEEDS_LOCALIZATION')
  result['rows'].append(dict(id=digest([source_id, sheet['sheet_id'], i])[:24], source_id=source_id,
      sheet_id=sheet['sheet_id'], sheet_name=sheet['title'], **info, row_id=f"{sheet['sheet_id']}!{i}:{i}", row_number=i,
      original_text=original, target_text=text if language == info['target_language'] else '', chinese_text=zh,
      localization_status='READY' if language==info['target_language'] else 'NEEDS_LOCALIZATION',
      text=original, text_zh=zh, language=language, reference_id=get('id') or f'R{i}', copy_type=get('type'),
      title=get('title'), performance=get('performance'), notes=get('notes'), source=sheet['title'],
      source_country=declared, fields=raw, warnings=warnings, last_synced_at=synced))
 result['warnings'] = sorted({w for r in result['rows'] for w in r['warnings']})
 return result

class FeishuAPI:
 def __init__(self, keychain, transport=None):
  self.keychain = keychain; self.transport = transport; self.token = ''; self.expires = 0; self.lock = threading.RLock()
 def reset(self): self.token = ''; self.expires = 0
 def request(self, method, path, **kwargs):
  try:
   with httpx.Client(timeout=httpx.Timeout(40, connect=10), follow_redirects=False, trust_env=False, transport=self.transport) as client:
    r = client.request(method, 'https://open.feishu.cn/open-apis'+path, **kwargs)
    if r.status_code == 429: raise ValueError('FEISHU_RATE_LIMIT')
    data = r.json(); code = data.get('code', -1)
    if r.status_code in {401, 403} or code in {99991663, 99991668, 99991672, 99991679}: raise ValueError('FEISHU_PERMISSION_REQUIRED')
    if code in {99991661, 99991664, 99991665, 99991671, 99991677}: raise ValueError('FEISHU_AUTH_FAILED')
    if r.status_code >= 400 or code != 0:
     if any(word in str(data.get('msg', '')).lower() for word in ('permission', 'forbidden', 'access denied')): raise ValueError('FEISHU_PERMISSION_REQUIRED')
     raise ValueError('FEISHU_API_ERROR')
    return data
  except httpx.HTTPError: raise ValueError('FEISHU_NETWORK_ERROR') from None
  except (TypeError, json.JSONDecodeError): raise ValueError('FEISHU_API_ERROR') from None
 def access_token(self):
  with self.lock:
   if self.token and time.time() < self.expires: return self.token
   app_id = self.keychain.get('FEISHU_APP_ID'); secret = self.keychain.get('FEISHU_APP_SECRET')
   if not app_id or not secret: raise ValueError('FEISHU_AUTH_REQUIRED')
   data = self.request('POST', '/auth/v3/tenant_access_token/internal', json=dict(app_id=app_id, app_secret=secret))
   self.token = data.get('tenant_access_token', ''); self.expires = time.time()+max(0, data.get('expire', 0)-120)
   if not self.token: raise ValueError('FEISHU_AUTH_FAILED')
   return self.token
 def get(self, path, params=None):
  return self.request('GET', path, params=params, headers={'Authorization':'Bearer '+self.access_token()}).get('data', {})
 def resolve(self, link):
  if link['link_type'] == 'wiki':
   node = self.get('/wiki/v2/spaces/get_node', {'token':link['node_token']}).get('node', {})
   if node.get('obj_type') != 'sheet': raise ValueError('FEISHU_UNSUPPORTED_TYPE')
   token = node.get('obj_token'); title = node.get('title', '')
  else: token = link['spreadsheet_token']; title = ''
  if not token or not re.fullmatch(r'[A-Za-z0-9]+', token): raise ValueError('FEISHU_API_ERROR')
  sheets = self.get(f'/sheets/v3/spreadsheets/{token}/sheets/query').get('sheets')
  if not isinstance(sheets, list): raise ValueError('FEISHU_API_ERROR')
  if link['selected_sheet_id'] and not any(s.get('sheet_id') == link['selected_sheet_id'] for s in sheets): raise ValueError('FEISHU_SHEET_NOT_FOUND')
  return dict(document_type='sheet', spreadsheet_token=token, document_title=title, sheets=sheets)
 def values(self, token, sheet):
  grid = sheet.get('grid_properties', {}); rows = int(grid.get('row_count', 0)); cols = int(grid.get('column_count', 0))
  if not rows or not cols: return []
  if rows*cols > 2000000 or cols > 256: raise ValueError('FEISHU_TOO_LARGE')
  name = ''; n = cols
  while n: n, mod = divmod(n-1, 26); name = chr(65+mod)+name
  page = max(1, min(500, 10000//cols)); out = []
  for start in range(1, rows+1, page):
   end = min(rows, start+page-1)
   data = self.get(f"/sheets/v2/spreadsheets/{token}/values/{sheet['sheet_id']}!A{start}:{name}{end}",
                   {'valueRenderOption':'ToString', 'dateTimeRenderOption':'FormattedString'})
   chunk = data.get('valueRange', {}).get('values')
   if not isinstance(chunk, list): raise ValueError('FEISHU_API_ERROR')
   out.extend(chunk); out.extend([[] for _ in range(end-start+1-len(chunk))])
  return out

class FeishuCopySource:
 def __init__(self, home, keychain, api=None):
  self.root = Path(home)/'copy_sources/feishu'; self.keychain = keychain; self.api = api or FeishuAPI(keychain); self.lock = threading.RLock()
 def read(self, name, default=None):
  try: return json.loads((self.root/name).read_text())
  except FileNotFoundError: return default
 def write(self, name, data):
  self.root.mkdir(mode=0o700, parents=True, exist_ok=True); p = self.root/(uuid.uuid4().hex+'.tmp')
  try:
   with p.open('x') as f:
    os.chmod(p, 0o600); json.dump(data, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
   p.replace(self.root/name)
  finally: p.unlink(missing_ok=True)
 def bind(self, name, url):
  if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('INVALID_REQUEST')
  link = parse_link(url)
  with self.lock:
   old = self.read('binding.json', {})
   b = dict(old if old.get('source_id') == link['source_id'] else {}, **link, name=name.strip())
   if old.get('url') != url: b.update(connection_status='UNTESTED', last_error=None)
   self.write('binding.json', b)
  return self.status()
 def binding(self):
  b = self.read('binding.json')
  if not b: raise ValueError('FEISHU_NOT_BOUND')
  return b
 def cache(self):
  b = self.binding(); c = self.read(b['source_id']+'.json')
  if not c: raise ValueError('FEISHU_NOT_SYNCED')
  return c
 def status(self):
  b = self.read('binding.json'); configured = all(self.keychain.configured(k) for k in ['FEISHU_APP_ID','FEISHU_APP_SECRET'])
  if not b: return dict(binding=None, credentials_configured=configured)
  c = self.read(b['source_id']+'.json', {}); sheets = c.get('sheets', [])
  return dict(binding=b, credentials_configured=configured, sheet_count=len(sheets) if c else len(b.get('sheets', [])),
      product_count=len({(s['market'],s['product_name']) for s in sheets}),
      br_count=len({s['product_name'] for s in sheets if s['market']=='Brazil'}),
      mx_count=len({s['product_name'] for s in sheets if s['market']=='Mexico'}),
      copy_count=sum(len(s['rows']) for s in sheets), last_synced_at=c.get('last_synced_at'),
      sheets=[{k:v for k,v in s.items() if k!='rows'}|dict(copy_count=len(s['rows'])) for s in sheets])
 def connect(self, sync=False):
  if not self.lock.acquire(blocking=False): raise ValueError('FEISHU_SYNC_BUSY')
  try:
   b = self.binding()
   try:
    doc = self.api.resolve(b)
    # Probe the requested sheet's real cells before reporting a connection.
    first = next((s for s in doc['sheets'] if s['sheet_id']==b['selected_sheet_id']), next(iter(doc['sheets']), None))
    if not first: raise ValueError('FEISHU_SHEET_NOT_FOUND')
    first_values = self.api.values(doc['spreadsheet_token'], first)
    now = time.time(); b.update(doc, connection_status='CONNECTED', last_error=None, last_checked_at=now)
    if sync:
     sheets = [normalize(b['source_id'], s, first_values if s['sheet_id']==first['sheet_id'] else self.api.values(doc['spreadsheet_token'], s), now) for s in doc['sheets']]
     self.write(b['source_id']+'.json', dict(source_id=b['source_id'], spreadsheet_token=doc['spreadsheet_token'], last_synced_at=now, sheets=sheets))
    self.write('binding.json', b)
   except ValueError as exc:
    b.update(connection_status='ERROR', last_error=str(exc) if str(exc) in ERRORS else 'FEISHU_API_ERROR'); self.write('binding.json', b); raise
   return self.status()
  finally: self.lock.release()
 def unbind(self):
  with self.lock: (self.root/'binding.json').unlink(missing_ok=True)
  return self.status()
 def matches(self, product, market):
  def key(v): return re.sub(r'[\s_＿-]+','',v).casefold()
  result = []; needle = key(product)
  if not needle: return []
  for s in self.cache()['sheets']:
   if s['market'] not in (None, market): continue
   name = key(s['product_name'])
   if needle == name or needle in name or name in needle:
    result.append(dict(sheet_id=s['sheet_id'],sheet_name=s['sheet_name'],market=s['market'],product_name=s['product_name'],copy_count=len(s['rows']),exact=needle==name))
  return result
 def reference(self, source_id, sheet_id, market, selected=None):
  c = self.cache()
  if c['source_id'] != source_id: raise ValueError('FEISHU_NOT_BOUND')
  s = next((s for s in c['sheets'] if s['sheet_id']==sheet_id), None)
  if s is None: raise ValueError('FEISHU_SHEET_NOT_FOUND')
  if s['market'] and s['market'] != market: raise ValueError('FEISHU_MARKET_MISMATCH')
  rows = copy.deepcopy(s['rows'])
  if selected is not None:
   ids=[selected] if isinstance(selected,str) else selected
   by_id={r['id']:r for r in rows}
   if not ids or any(i not in by_id for i in ids):raise ValueError('SCRIPT_SELECTION_INVALID')
   rows=[by_id[i] for i in dict.fromkeys(ids)]
  return dict(source_type='feishu', source_id=source_id, sheet_id=sheet_id, filename=s['sheet_name'],
              headers=s['headers'], rows=[r['fields'] for r in rows], script_rows=rows, count=len(rows), last_synced_at=c['last_synced_at'])
