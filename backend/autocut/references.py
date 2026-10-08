import csv,io
from pathlib import Path

def parse(path):
 p=Path(path)
 if p.stat().st_size>15*1024*1024:raise ValueError('SCRIPT_TABLE_INVALID')
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
