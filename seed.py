"""Idempotent DQ/demo fact seeding; NEVER insert into existing PRJ reference masters."""
from decimal import Decimal
from sqlalchemy import select
from common.database import SessionLocal, init_db
from common.models import FinancialFact, Rule, Execution, Schedule, ImportBatch
from common.reference_data import attributes, portfolios
from common.schemas import RuleInput
from common.services import save_rule

def seed():
    init_db()
    with SessionLocal() as db:
        active_attrs = attributes(db, 'Y')
        active_ports = [p for p in portfolios(db) if p.is_active]
        if not active_attrs or not active_ports:
            # No artificial master records: the owner provisions these tables separately.
            return {'seeded': False, 'reason': 'Populate existing PRJ attribute and portfolio reference tables first.'}
        code = active_ports[0].short_code
        targets = [a.prj_id for a in active_attrs[:2]]
        if not db.scalar(select(FinancialFact.id).where(FinancialFact.document_id == 'DEMO-001').limit(1)):
            rows = [
                (targets[0], '2025-12', '8.4', 'PERCENT'),
                (targets[0], '2026-12', '5.8', 'PERCENT'),
            ]
            if len(targets)>1:
                rows += [(targets[1],'2025-12','100','MILLION'),(targets[1],'2026-12','170','MILLION')]
            db.add_all(FinancialFact(document_id='DEMO-001', company='DQ Sample Company', portfolio_code=code,
                  prj_id=p, period=period, value=Decimal(val), unit=unit, currency='INR')
                  for p,period,val,unit in rows)
            db.commit()
        if not db.get(Rule, 'Rule_06'):
            save_rule(db,RuleInput(id='Rule_06',name='Sample ratio not below 7%',rule_type='LIMIT',
                 targets=[targets[0]],portfolios=[code],status='ACTIVE',
                 config={'operator':'>=','threshold':'7','threshold_value':'7','threshold_type':'PERCENTAGE','require_cde':True},severity='HIGH'))
        if len(targets)>1 and not db.get(Rule,'Rule_09'):
            save_rule(db,RuleInput(id='Rule_09',name='Sample movement above 50% flag',rule_type='MOVEMENT',
                 targets=[targets[1]],portfolios=[code],status='ACTIVE',
                 config={'operator':'>','threshold':'50','threshold_value':'50','threshold_type':'PERCENTAGE','comparison_period':'PREVIOUS_YEAR','direction':'BOTH'},severity='HIGH'))
        # Additional demo scenarios, isolated by company/document to make each check reproducible.
        # Reference/master tables are never seeded or modified.
        demo_cases = [
            ('MANDATORY', targets[0], 'DQ-DEMO-MANDATORY', 'DQ Mandatory Demo', None, 'AMOUNT', 'INR'),
            ('ENUMERATION', targets[0], 'DQ-DEMO-ENUM', 'DQ Enumeration Demo', '18', 'AMOUNT', 'ZZZ'),
            ('PATTERN', targets[0], 'bad-document-id', 'DQ Pattern Demo', '12', 'AMOUNT', 'INR'),
            ('DUPLICATE', targets[0], 'DQ-DEMO-DUP', 'DQ Duplicate Demo', '100', 'AMOUNT', 'INR'),
            ('LIMIT', targets[0], 'DQ-DEMO-LIMIT', 'DQ Limit Demo', '5.8', 'PERCENT', 'INR'),
            ('MOVEMENT', targets[0], 'DQ-DEMO-MOVE', 'DQ Movement Demo', '170', 'AMOUNT', 'INR'),
        ]
        for typ,target,doc,company,val,unit,currency in demo_cases:
            if not db.scalar(select(FinancialFact.id).where(FinancialFact.document_id==doc).limit(1)):
                db.add(FinancialFact(document_id=doc,company=company,portfolio_code=code,
                    prj_id=target,period='2026-12',value=Decimal(val) if val is not None else None,
                    unit=unit,currency=currency))
                if typ=='DUPLICATE':
                    db.add(FinancialFact(document_id=doc,company=company,portfolio_code=code,
                        prj_id=target,period='2026-12',value=Decimal('101'),unit=unit,currency=currency))
                if typ=='MOVEMENT':
                    db.add(FinancialFact(document_id=doc,company=company,portfolio_code=code,
                        prj_id=target,period='2025-12',value=Decimal('100'),unit=unit,currency=currency))
        if len(targets)>1 and not db.scalar(select(FinancialFact.id).where(FinancialFact.document_id=='DQ-DEMO-COMP').limit(1)):
            for target,amount in [(targets[0],'200'),(targets[1],'100')]:
                db.add(FinancialFact(document_id='DQ-DEMO-COMP',company='DQ Comparison Demo',portfolio_code=code,
                    prj_id=target,period='2026-12',value=Decimal(amount),unit='AMOUNT',currency='INR'))
            for target,amount in [(targets[0],'200'),(targets[1],'198')]:
                db.add(FinancialFact(document_id='DQ-DEMO-RECON',company='DQ Reconciliation Demo',portfolio_code=code,
                    prj_id=target,period='2026-12',value=Decimal(amount),unit='AMOUNT',currency='INR'))
        db.commit()
        scenarios=[
            ('Rule_DEMO_MANDATORY','Mandatory value check','MANDATORY',targets[0],{'require_cde':True}),
            ('Rule_DEMO_DUPLICATE','Maximum one record per CDE','DUPLICATE',targets[0],{'operator':'<=','threshold_value':'1','threshold_type':'COUNT'}),
            ('Rule_DEMO_ENUMERATION','Currency must be INR/USD','ENUMERATION',targets[0],{'field':'currency','allowed':['INR','USD']}),
            ('Rule_DEMO_PATTERN','Document ID must start DQ','PATTERN',targets[0],{'field':'document_id','pattern':r'DQ-[A-Z0-9-]+'}),
            ('Rule_DEMO_LIMIT','Value must be at least 7%','LIMIT',targets[0],{'operator':'>=','threshold_value':'7','threshold_type':'PERCENTAGE'}),
            ('Rule_DEMO_MOVEMENT','Movement over 50%','MOVEMENT',targets[0],{'operator':'>','threshold_value':'50','threshold_type':'PERCENTAGE','comparison_period':'PREVIOUS_YEAR','direction':'BOTH'}),
        ]
        if len(targets)>1:
            scenarios += [
                ('Rule_DEMO_COMPARISON','First amount greater than second','COMPARISON',targets[0],{'operator':'>','other_prj_id':targets[1],'tolerance':'0'}),
                ('Rule_DEMO_RECON','Amounts reconcile within 1','RECONCILIATION',targets[0],{'operator':'=','other_prj_id':targets[1],'tolerance':'1','threshold_value':'1','threshold_type':'AMOUNT'}),
            ]
        for rid,name,kind,target,cfg in scenarios:
            if not db.get(Rule,rid):
                save_rule(db,RuleInput(id=rid,name=name,rule_type=kind,targets=[target],portfolios=[code],
                     status='ACTIVE',severity='HIGH',config=cfg))
        # Populate DQ orchestration and results with one sample run, only on explicit opt-in.
        # Do not create fake email-delivery records or modify PRJ reference masters.
        if not db.get(Execution, 'DEMO-SEED-RUN'):
            db.add(Execution(id='DEMO-SEED-RUN', scope={'period':'2026-12','portfolio':code}))
            db.commit()
        if not db.scalar(select(Schedule.id).where(Schedule.name=='DQ sample monthly (disabled)')):
            db.add(Schedule(name='DQ sample monthly (disabled)',frequency='MONTHLY',
                hour_utc=1,minute_utc=0,day_of_month=1,rule_ids=['Rule_06','Rule_09'], active=False))
            db.commit()
        if not db.get(ImportBatch, 'DEMO-IMPORT'):
            db.add(ImportBatch(id='DEMO-IMPORT',filename='sample_rules.xlsx',candidates=[
                {'rule_id':'Rule_06','prj_id':targets[0],'status':'DRAFT_REVIEW',
                 'description':'Not below 7%'}]))
            db.commit()
    from common.execution import execute
    execute('DEMO-SEED-RUN')  # idempotent: only processes QUEUED runs
    return {'seeded': True, 'portfolio': code, 'target_prj_ids': targets, 'document_id': 'DEMO-001', 'reporting_period': '2026-12', 'sample_rules': ['Rule_06','Rule_09'] if len(targets)>1 else ['Rule_06'], 'scenario_rules': [x[0] for x in scenarios], 'execution_id': 'DEMO-SEED-RUN'}


if __name__=='__main__': print(seed())
