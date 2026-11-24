"""Read-only adapters for the EXISTING PRJ attribute and portfolio reference tables.

The real tables are reflected, not created/migrated or written by this application.
A few display fields are optional because legacy schema variants differ.
"""
from dataclasses import dataclass
from functools import lru_cache
from sqlalchemy import Boolean, MetaData, Table, select
from sqlalchemy.orm import Session
from common.database import engine

ATTRIBUTE_TABLE = 'prj_attribute_master'
PORTFOLIO_TABLE = 'prj_portfolio_reference'

@dataclass(frozen=True)
class AttributeRecord:
    prj_id: str
    name: str
    is_cde: str
    cfi_id: str | None = None

@dataclass(frozen=True)
class PortfolioRecord:
    short_code: str
    portfolio_name: str
    sector_name: str | None = None
    sub_sector: str | None = None
    is_active: bool = True
    document_type_id: str | None = None

@lru_cache(maxsize=2)
def reference_table(name: str):
    if name not in (ATTRIBUTE_TABLE, PORTFOLIO_TABLE):
        raise ValueError('Unsupported reference table')
    return Table(name, MetaData(), autoload_with=engine)

def _col(table, *aliases):
    lookup = {c.name.lower(): c for c in table.columns}
    return next((lookup[x.lower()] for x in aliases if x.lower() in lookup), None)

def _value(row, column, default=None):
    return row._mapping[column] if column is not None else default

def _cde_label(value) -> str:
    """Expose a stable Y/N API regardless of Boolean or legacy text storage."""
    if isinstance(value, bool):
        return 'Y' if value else 'N'
    if value is None:
        return 'N'
    return 'Y' if str(value).strip().upper() in ('Y', 'YES', 'TRUE', 'T', '1') else 'N'


def _cde_filter_value(column, value):
    """Bind a Boolean for a Boolean DB column, not a VARCHAR ('Y'/'N')."""
    label = _cde_label(value)
    if isinstance(column.type, Boolean):
        return label == 'Y'
    return label


def _attribute(row, t):
    prj = _col(t, 'prj_id')
    name = _col(t, 'attribute_name', 'prj_item_name', 'name', 'description')
    cde = _col(t, 'is_cde')
    cfi = _col(t, 'cfi_id')
    key = str(_value(row, prj))
    return AttributeRecord(key, str(_value(row, name, key) or key), _cde_label(_value(row, cde, None)), str(_value(row, cfi)) if _value(row, cfi) is not None else None)

def attributes(db: Session, is_cde=None, limit=10000):
    t = reference_table(ATTRIBUTE_TABLE)
    cde = _col(t, 'is_cde')
    query = select(t)
    if is_cde is not None:
        if cde is None:
            raise ValueError('prj_attribute_master is missing is_cde')
        query = query.where(cde == _cde_filter_value(cde, is_cde))
    return [_attribute(r,t) for r in db.execute(query.limit(limit)).all()]

def get_attribute(db: Session, prj_id: str):
    t = reference_table(ATTRIBUTE_TABLE)
    row = db.execute(select(t).where(_col(t,'prj_id') == prj_id).limit(1)).first()
    return _attribute(row,t) if row is not None else None

def _portfolio(row,t):
    code = _col(t, 'short_code')
    name = _col(t, 'portfolio_name')
    sector = _col(t, 'sector_name')
    sub = _col(t, 'sub_sector')
    active = _col(t, 'is_active')
    doc = _col(t, 'document_type_id')
    status = _value(row,active, True)
    enabled = status if isinstance(status,bool) else str(status or '').strip().upper() in ('Y','YES','TRUE','1')
    return PortfolioRecord(str(_value(row,code)), str(_value(row,name) or _value(row,code)), _value(row,sector), _value(row,sub), enabled, str(_value(row,doc)) if _value(row,doc) is not None else None)

def portfolios(db: Session, limit=10000):
    t = reference_table(PORTFOLIO_TABLE)
    return [_portfolio(r,t) for r in db.execute(select(t).limit(limit)).all()]

def get_portfolio(db: Session, short_code: str):
    t = reference_table(PORTFOLIO_TABLE)
    row = db.execute(select(t).where(_col(t,'short_code') == short_code).limit(1)).first()
    return _portfolio(row,t) if row is not None else None


def promote_cde(db: Session, prj_id: str):
    """Explicitly approved import operation; never run during preview or startup.

    Returns old/new classifications; requires UPDATE privileges on the master table.
    """
    from sqlalchemy import update
    t=reference_table(ATTRIBUTE_TABLE)
    field=_col(t,'is_cde')
    pid=_col(t,'prj_id')
    if field is None or pid is None: raise ValueError('Attribute master columns unavailable')
    old=get_attribute(db,prj_id)
    if old is None: raise ValueError('Unknown PRJ ID '+prj_id)
    if old.is_cde=='Y':return {'prj_id':prj_id,'old':'Y','new':'Y','changed':False}
    value=True if isinstance(field.type,Boolean) else 'Y'
    db.execute(update(t).where(pid==prj_id).values({field:value}))
    return {'prj_id':prj_id,'old':old.is_cde,'new':'Y','changed':True}
