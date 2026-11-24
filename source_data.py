"""Financial source adapters. No database DDL; UNS mapping is explicit and read-only."""
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from sqlalchemy import MetaData, Table, select, and_
from common.database import engine
from common.models import FinancialFact

SOURCES = ('DEMO_NORMALIZED', 'UNS_FINANCIAL_LINE_ITEMS')

@dataclass
class FinancialRow:
    id: object
    document_id: str
    company: str
    portfolio_code: str
    prj_id: str
    period: str
    value: object
    currency: str
    unit: str
    collection_type: str

@lru_cache(maxsize=1)
def uns_table():
    return Table(os.getenv('UNS_TABLE','uns_financial_line_items'), MetaData(), autoload_with=engine)

def uns_columns():
    table = uns_table()
    by_name = {col.name.lower():col for col in table.columns}
    mapping = {}
    required = ('prj_id','value','period','company','portfolio_code')
    aliases = {'company':('filing_company','company'), 'portfolio_code':('portfolio_code','short_code'),
               'document_id':('document_id',), 'collection_type':('data_collection_type','collection_type'),
               'id':('line_item_id','id'), 'currency':('currency',), 'unit':('unit',)}
    for key in ('id','document_id','company','portfolio_code','prj_id','period','value','currency','unit','collection_type'):
        candidates = (os.getenv('UNS_COL_'+key.upper()),) if os.getenv('UNS_COL_'+key.upper()) else (key, *aliases.get(key,()))
        mapping[key] = next((by_name[n.lower()] for n in candidates if n and n.lower() in by_name), None)
    missing = [c for c in required if mapping[c] is None]
    if missing:
        raise ValueError('UNS source requires field mappings for: '+', '.join(missing)+'. Set UNS_COL_* in .env or provide a read-only database view.')
    return table,mapping

def load_rows(db, scope, *, include_history=False):
    source = scope.get('source') or 'DEMO_NORMALIZED'
    if source not in SOURCES: raise ValueError('Unsupported financial source')
    if source == 'DEMO_NORMALIZED':
        q=select(FinancialFact)
        filters = [('portfolio_code','portfolio'),('company','company'),('document_id','document_id')]
        if not include_history: filters += [('period','period')]
        for attr,key in filters:
            if scope.get(key): q=q.where(getattr(FinancialFact,attr)==scope[key])
        return db.scalars(q).all()
    table,m = uns_columns()
    query = select(table)
    for col,key in [('portfolio_code','portfolio'),('company','company'),('document_id','document_id')]:
        if scope.get(key):
            if m[col] is None: raise ValueError('UNS mapping missing scoped column '+col)
            query=query.where(m[col]==scope[key])
    if scope.get('period') and not include_history: query=query.where(m['period']==scope['period'])
    result = []
    for row in db.execute(query).all():
        def v(key, default=None):
            return row._mapping[m[key]] if m[key] is not None else default
        raw=v('value')
        try: amount=Decimal(str(raw)) if raw is not None else None
        except (InvalidOperation, ValueError): amount=None
        result.append(FinancialRow(v('id'),str(v('document_id','') or ''),str(v('company')),str(v('portfolio_code')),
            str(v('prj_id')),str(v('period')),amount,str(v('currency','INR') or 'INR'),
            str(v('unit','AMOUNT') or 'AMOUNT'),str(v('collection_type','NON_AR') or 'NON_AR')))
    return result
