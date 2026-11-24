from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections import defaultdict
import re
from sqlalchemy import select
from common.database import SessionLocal
from common.models import Rule, FinancialFact, Execution, Result
from common.services import version_for
from common.reference_data import get_attribute, get_portfolio
from common.source_data import load_rows


def dec(x):
    try: return Decimal(str(x)) if x is not None else None
    except (InvalidOperation, ValueError): return None

def passes(actual, operator, threshold):
    if operator == 'IS_NULL': return actual is None
    if operator == 'IS_NOT_NULL': return actual is not None
    if actual is None: return False
    if operator in ('IN','NOT_IN'):
        return (actual in threshold) if operator=='IN' else (actual not in threshold)
    if operator in ('BETWEEN','NOT_BETWEEN'):
        x,y=map(dec,threshold)
        ok=x<=dec(actual)<=y
        return ok if operator=='BETWEEN' else not ok
    y=dec(threshold); x=dec(actual)
    if x is None or y is None: return False
    return {'>':lambda:x>y,'>=':lambda:x>=y,'<':lambda:x<y,'<=':lambda:x<=y,'=':lambda:x==y,'!=':lambda:x!=y}[operator]()

def period_predecessor(period, kind):
    # ISO reporting month or ISO date. Explicit calendar offset, not lexicographic predecessor.
    try:
        y,m=map(int,period[:7].split('-'))
        step={'PREVIOUS_MONTH':1,'PREVIOUS_QUARTER':3,'PREVIOUS_YEAR':12,'PREVIOUS_PERIOD':12}.get(kind,12)
        k=y*12 + m-1-step
        return f'{k//12:04d}-{k%12+1:02d}'
    except Exception: return None

def evaluate(kind, facts, fact, cfg, by_key):
    v=dec(fact.value)
    op=cfg.get('operator','>=')
    if kind=='MANDATORY': return v is not None,'Value must not be null',None,{}
    if kind=='DATA_TYPE': return v is not None,'Value must be numeric',None,{}
    if kind=='LIMIT':
        threshold=cfg.get('threshold_value', cfg.get('threshold'))
        threshold_type=cfg.get('threshold_type','AMOUNT')
        # PERCENTAGE assumes unit PERCENT. Fraction-style inputs are converted explicitly.
        measured=v
        if threshold_type == 'PERCENTAGE' and v is not None and fact.unit == 'FRACTION':
            measured=v*100
        elif threshold_type == 'PERCENTAGE' and fact.unit not in ('PERCENT', 'PERCENTAGE', 'FRACTION'):
            return None, 'Incompatible unit for percentage limit', str(threshold), {'unit':fact.unit}
        return passes(measured,op,threshold),f'Expected {op} {threshold} ({threshold_type})',str(threshold),{'threshold_type':threshold_type, 'evaluated_value':str(measured) if measured is not None else None}
    if kind=='ENUMERATION':
        field=cfg.get('field','currency')
        if field not in ('currency','unit','collection_type'): return False,'Unsupported enumeration field',None,{}
        actual=getattr(fact,field)
        return actual in cfg['allowed'],f'{field} must be one of {cfg["allowed"]}',str(cfg['allowed']),{'evaluated_value':actual}
    if kind=='PATTERN':
        field=cfg.get('field','currency')
        if field not in ('currency','unit','collection_type','document_id'): return False,'Unsupported pattern field',None,{}
        actual=str(getattr(fact,field) or '')
        return bool(re.fullmatch(cfg['pattern'],actual)),f'{field} must match pattern',cfg['pattern'],{'evaluated_value':actual}
    if kind=='DUPLICATE':
        peers=[x for x in facts if (x.company,x.portfolio_code,x.prj_id,x.period,x.collection_type)==(fact.company,fact.portfolio_code,fact.prj_id,fact.period,fact.collection_type)]
        # COUNT is a maximum allowed number of matching records by default.
        threshold=cfg.get('threshold_value',cfg.get('threshold','1'))
        comparator=cfg.get('operator','<=')
        return passes(len(peers),comparator,threshold),f'Count must be {comparator} {threshold}',str(threshold),{'count':len(peers),'threshold_type':'COUNT'}
    if kind=='MOVEMENT':
        previous_period=period_predecessor(fact.period,cfg.get('comparison_period','PREVIOUS_YEAR'))
        candidates=by_key.get((fact.company,fact.portfolio_code,fact.prj_id,previous_period,fact.collection_type),[])
        if len(candidates)!=1: return None,'Previous period missing or ambiguous',previous_period,{}
        prev=candidates[0]
        if (fact.currency,fact.unit)!=(prev.currency,prev.unit): return None,'Currency or unit differs across periods',previous_period,{}
        pv=dec(prev.value)
        if pv is None or pv==0 or v is None: return None,'Missing value or zero denominator',previous_period,{}
        movement=((v-pv)/abs(pv))*100
        mode=cfg.get('direction','BOTH')
        measured=abs(movement) if mode=='BOTH' else movement if mode=='INCREASE' else -movement
        # MOVEMENT operator denotes BREACH, not PASS.
        threshold=cfg.get('threshold_value',cfg.get('threshold'))
        breach=passes(measured,op,threshold)
        return not breach,f'Movement {movement:.2f}% (breach if {op} {threshold}%)',str(threshold),{'previous_value':str(pv),'movement_percent':str(movement),'previous_period':previous_period}
    if kind in ('COMPARISON','RECONCILIATION'):
        other=cfg['other_prj_id']
        peers=by_key.get((fact.company,fact.portfolio_code,other,fact.period,fact.collection_type),[])
        if len(peers)!=1: return None,'Comparison operand missing or ambiguous',other,{}
        peer=peers[0]
        if (fact.currency,fact.unit)!=(peer.currency,peer.unit): return None,'Currency or unit mismatch',other,{}
        ov=dec(peer.value)
        if v is None or ov is None: return None,'Missing comparison value',other,{}
        tolerance=dec(cfg.get('tolerance',0)) or Decimal(0)
        if kind=='RECONCILIATION': return abs(v-ov)<=tolerance,f'Difference exceeds tolerance {tolerance}',str(ov),{'difference':str(v-ov)}
        return passes(v,op,ov),f'Expected {op} {other}',str(ov),{}
    return None,'No handler available',None,{}

def execute(execution_id):
    with SessionLocal() as db:
        run=db.get(Execution,execution_id)
        if not run or run.status!='QUEUED': return
        run.status='RUNNING'; db.commit()
        try:
            scope=run.scope or {}
            current=load_rows(db,scope)
            # History must be loaded from the same source (including prior periods).
            history=load_rows(db,scope,include_history=True)
            by_key=defaultdict(list)
            for fact in history: by_key[(fact.company,fact.portfolio_code,fact.prj_id,fact.period,fact.collection_type)].append(fact)
            rule_query=select(Rule).where(Rule.status=='ACTIVE')
            if scope.get('cde_only') and not scope.get('portfolio'):
                raise ValueError('CDE-driven execution requires a selected portfolio')
            if scope.get('rule_ids'): rule_query=rule_query.where(Rule.id.in_(scope['rule_ids']))
            for rule in db.scalars(rule_query).all():
                version=version_for(db,rule)
                if version is None:
                    db.add(Result(execution_id=run.id, rule_id=rule.id,
                        rule_version=rule.current_version or 0, prj_id='UNCONFIGURED',
                        outcome='ERROR', reason='MISSING_RULE_VERSION: rule has no stored configuration',
                        evidence={}, rule_snapshot={'error': 'MISSING_RULE_VERSION'}))
                    db.commit()
                    continue
                cfg=version.config
                for target in version.targets:
                    attr=get_attribute(db,target)
                    if scope.get('cde_only') and (not attr or attr.is_cde != 'Y'):
                        continue
                    applicable=[f for f in current if f.prj_id==target]
                    snapshot={'version':version.version,'config':cfg,'targets':version.targets,'portfolios':version.portfolios,'cde_at_execution':attr.is_cde if attr else None,'source':scope.get('source','DEMO_NORMALIZED')}
                    def add(outcome,reason,fact=None,expected=None,evidence=None):
                        db.add(Result(execution_id=run.id,rule_id=rule.id,rule_version=version.version,prj_id=target,outcome=outcome,reason=reason,company=fact.company if fact else scope.get('company'),portfolio_code=fact.portfolio_code if fact else scope.get('portfolio'),period=fact.period if fact else scope.get('period'),document_id=fact.document_id if fact else scope.get('document_id'),actual=str(fact.value) if fact and fact.value is not None else None,expected=expected,evidence=evidence or {},rule_snapshot=snapshot))
                    if not attr or (cfg.get('require_cde',True) and attr.is_cde!='Y'):
                        add('SKIPPED','CDE_CLASSIFICATION_INACTIVE' if attr else 'UNKNOWN_ATTRIBUTE');continue
                    if not applicable:
                        add('NOT_APPLICABLE','No financial data in selected scope');continue
                    for fact in applicable:
                        portfolio=get_portfolio(db,fact.portfolio_code)
                        if (version.portfolios and fact.portfolio_code not in version.portfolios) or not portfolio or not portfolio.is_active:
                            add('SKIPPED','PORTFOLIO_NOT_APPLICABLE',fact);continue
                        if cfg.get('collection_type') and cfg['collection_type']!='ANY' and fact.collection_type!=cfg['collection_type']:
                            add('NOT_APPLICABLE','COLLECTION_TYPE_NOT_APPLICABLE',fact);continue
                        other=cfg.get('other_prj_id')
                        if other and cfg.get('require_cde',True):
                            other_attr=get_attribute(db,other)
                            if not other_attr or other_attr.is_cde!='Y':
                                add('SKIPPED','OPERAND_CDE_INACTIVE',fact);continue
                        try:
                            good,reason,expected,evidence=evaluate(rule.rule_type,history,fact,cfg,by_key)
                            add('PASS' if good is True else 'FAIL' if good is False else 'SKIPPED',reason,fact,expected,evidence)
                        except Exception as exc:
                            add('ERROR',f'Rule evaluation error: {type(exc).__name__}',fact)
                db.commit()
            run.status='COMPLETED';run.completed_at=datetime.now(timezone.utc);db.commit()
        except Exception as exc:
            db.rollback()
            run=db.get(Execution,execution_id);run.status='FAILED';run.error=f'{type(exc).__name__}: {exc}'[:2000];run.completed_at=datetime.now(timezone.utc);db.commit()
