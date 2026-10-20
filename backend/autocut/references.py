import csv,hashlib,io,os,tempfile
from pathlib import Path

MAX_TABLE_BYTES=15*1024*1024
TABLE_SUFFIXES={'.csv','.xlsx','.xls'}

def upload_name(filename):
 name=Path(filename.replace('\\','/')).name
 if Path(name).suffix.lower() not in TABLE_SUFFIXES:raise ValueError('SCRIPT_TABLE_FORMAT')
 if len(name.encode('utf-8'))>240 or any(ord(c)<32 for c in name):raise ValueError('SCRIPT_TABLE_FILENAME')
 return name

def import_upload(home,filename,data):
 """Validate a browser upload before admitting its private, durable copy."""
 name=upload_name(filename)
 if not data:raise ValueError('SCRIPT_TABLE_EMPTY')
 if len(data)>MAX_TABLE_BYTES:raise ValueError('SCRIPT_TABLE_TOO_LARGE')
 root=Path(home)/'imports'/'references';root.mkdir(parents=True,exist_ok=True,mode=0o700)
 with tempfile.TemporaryDirectory(prefix='.upload-',dir=root) as temp:
  staged=Path(temp)/name;staged.write_bytes(data);staged.chmod(0o600)
  result=parse(staged)
  folder=root/hashlib.sha256(data).hexdigest();folder.mkdir(exist_ok=True,mode=0o700)
  path=folder/name
  if not path.exists():os.replace(staged,path)
  elif path.is_symlink() or hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(data).digest():raise ValueError('SCRIPT_TABLE_CACHE_INVALID')
 return str(path.resolve()),result

def parse(path):
 p=Path(path)
 if p.stat().st_size>MAX_TABLE_BYTES:raise ValueError('SCRIPT_TABLE_TOO_LARGE')
 try:
  if p.suffix.lower()=='.csv':
   raw=p.read_bytes();text=None
   for enc in ['utf-8-sig','utf-16','gb18030']:
    try:text=raw.decode(enc);break
    except UnicodeError:pass
   if text is None:raise ValueError()
   try:dialect=csv.Sniffer().sniff(text[:4096],delimiters=',\t;')
   except csv.Error:dialect=csv.excel
   rows=list(csv.reader(io.StringIO(text),dialect))
  elif p.suffix.lower()=='.xlsx':
   from openpyxl import load_workbook
   book=load_workbook(p,read_only=True,data_only=True);rows=list(book.active.values);book.close()
  elif p.suffix.lower()=='.xls':
   import xlrd
   book=xlrd.open_workbook(p);sheet=book.sheet_by_index(0);rows=[sheet.row_values(i) for i in range(min(2001,sheet.nrows))]
  else:raise ValueError()
  rows=[[str(v or '').strip() for v in row] for row in rows if any(v is not None and str(v).strip() for v in row)]
  if len(rows)<2 or len(rows)>2001 or len(rows[0])>60:raise ValueError()
  headers=rows[0]
  return dict(filename=p.name,headers=headers,rows=[dict(zip(headers,r)) for r in rows[1:]],count=len(rows)-1)
 except Exception:raise ValueError('SCRIPT_TABLE_INVALID') from None
