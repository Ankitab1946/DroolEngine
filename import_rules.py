"""Parse grouped banking/insurance business rule spreadsheets; preview is read-only."""
import csv
import hashlib
import io
import re
import unicodedata
from decimal import Decimal
from openpyxl import load_workbook
from sqlalchemy import select
from common.models import Rule
from common.reference_data import get_attribute

MAX_SIZE=30*1024*1024
MAX_ROWS=15000

def clean(value): return str(value).strip() if value is not None else ''
def norm(value): return re.sub(r'[^a-z0-9]+',' ',clean(value).lower()).strip()
def rule_config(text):
    raw=clean(text); t=raw.lower()
    match=re.search(r'(-?\d+(?:\.\d+)?)\s*%',t)
    if not raw: return None
    if not match: return {'rule_type':'MANUAL_REVIEW','config':{},'reason':'Threshold or syntax requires manual review'}
    threshold=match.group(1)
    if 'movement' in t or 'increase' in t or 'decrease' in t:
        return {'rule_type':'MOVEMENT','config':{'operator':'>' if '>' in t or 'above' in t or 'increase' in t else '<',
            'threshold_value':threshold,'threshold_type':'PERCENTAGE','comparison_period':'PREVIOUS_YEAR','direction':'BOTH'}}
    if 'not below' in t or 'at least' in t: op='>='
    elif 'not above' in t or 'at most' in t: op='<='
    elif 'below' in t or '<' in t: op='<'
    elif 'above' in t or '>' in t: op='>'
    else: return {'rule_type':'MANUAL_REVIEW','config':{},'reason':'Ambiguous limit operator'}
    return {'rule_type':'LIMIT','config':{'operator':op,'threshold_value':threshold,'threshold_type':'PERCENTAGE'}}

def workbook_rows(blob,filename):
    suffix=filename.lower().rsplit('.',1)[-1]
    if suffix=='xlsx':
        wb=load_workbook(io.BytesIO(blob),read_only=True,data_only=True)
        try:
            return [list(r) for r in wb.active.iter_rows(values_only=True)]
        finally:wb.close()
    if suffix=='xls':
        import xlrd
        book=xlrd.open_workbook(file_contents=blob)
        sh=book.sheet_by_index(0)
        return [sh.row_values(i) for i in range(sh.nrows)]
    if suffix in ('csv','prj'):
        data=blob.decode('utf-8-sig')
        return list(csv.reader(io.StringIO(data)))
    raise ValueError('Supported formats: .xlsx, .xls, .csv (or .prj text CSV)')

def build_candidates(db,blob,filename):
    if len(blob)>MAX_SIZE: raise ValueError('Workbook too large (30 MB maximum)')
    rows=workbook_rows(blob,filename)
    if len(rows)>MAX_ROWS+3: raise ValueError('Too many rows (15000 maximum)')
    if not rows: raise ValueError('Empty workbook')
    # Template: row 1 portfolio groups; row 2 columns, or a normal single header.
    group_row=rows[0]; grouped=len(rows)>1 and ('prj id' in norm(' '.join(map(clean,rows[1])))) and ('bank' in norm(' '.join(map(clean,group_row))) or 'insurance' in norm(' '.join(map(clean,group_row))))
    headers=rows[1] if grouped else rows[0]
    groups=[]; active='ALL'
    for i,h in enumerate(headers):
        if grouped and i<len(group_row) and clean(group_row[i]):
            if 'bank' in norm(group_row[i]):active='BANK'
            elif 'insurance' in norm(group_row[i]):active='INSURANCE'
        groups.append(active)
    descriptions=[i for i,h in enumerate(headers) if 'proposed business rule' in norm(h)]
    if not descriptions:raise ValueError('Missing Proposed Business Rule column')
    descidx=descriptions[0]
    cdeidx=next((i for i,h in enumerate(headers) if norm(h)=='cde'),None)
    candidates=[];seen=set()
    existing_names={norm(r.name) for r in db.scalars(select(Rule)).all()}
    for row_index,row in enumerate(rows[2 if grouped else 1:],start=3 if grouped else 2):
        desc=clean(row[descidx]) if descidx<len(row) else ''
        if not desc:continue
        parsed=rule_config(desc)
        attribute=clean(row[cdeidx]) if cdeidx is not None and cdeidx<len(row) else ''
        for i,h in enumerate(headers):
            if 'prj id' not in norm(h):continue
            raw=clean(row[i]) if i<len(row) else ''
            if not raw or raw in ('-','N/A'):continue
            for pid in re.split(r'[,;|\n]+',raw):
                pid=pid.strip()
                if not pid:continue
                ar='AR' if ' ar ' in (' '+norm(h)+' ') and 'non' not in norm(h) else 'NON_AR'
                portfolio=groups[i]
                fp='|'.join((norm(desc),norm(attribute),pid.upper(),portfolio,ar))
                if fp in seen: continue
                seen.add(fp)
                digest=hashlib.sha256(fp.encode()).hexdigest()[:12].upper()
                rid='DQ_'+digest
                attr=get_attribute(db,pid)
                existing=bool(db.get(Rule,rid))
                candidates.append({'rule_id':rid,'prj_id':pid,'attribute_name':attribute,'description':desc,
                    'portfolio_group':portfolio,'collection_type':ar,'row':row_index,
                    'rule_type':parsed['rule_type'],'config':parsed['config'],'valid_attribute':bool(attr),
                    'is_cde':attr.is_cde if attr else None,'needs_cde_update':bool(attr and attr.is_cde!='Y'),
                    'status':'DUPLICATE_EXISTING' if existing else 'MANUAL_REVIEW' if parsed['rule_type']=='MANUAL_REVIEW' else 'UNKNOWN_PRJ_ID' if not attr else 'DRAFT_REVIEW'})
    return candidates
