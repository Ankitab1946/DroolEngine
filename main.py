import os, io, uuid, smtplib
from email.message import EmailMessage
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Header, Request
from fastapi.security import APIKeyHeader
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.openapi.docs import get_swagger_ui_html
from api.offline_docs import render_docs
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openpyxl import load_workbook
from common.database import SessionLocal,init_db
from common.models import FinancialFact,Rule,RuleVersion,RuleMapping,Execution,Schedule,ImportBatch,Notification,Result
from common.schemas import RuleInput,ExecutionInput,ScheduleInput,RULE_TYPES
from common.services import save_rule,serialize_rule,version_for,require_rule_version,validate_references,change_rule_status,serialize_history
from common.reporting import summary,excel_report
from common.execution import execute
from common.import_rules import build_candidates, MAX_SIZE
from common.reference_data import attributes as source_attributes, get_attribute, portfolios as source_portfolios, promote_cde

api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False, description='Value of DQ_API_KEY in .env')

def require_api_key(request: Request, x_api_key: str | None = Depends(api_key_header)):
    if request.url.path in ('/api-reference', '/docs', '/swagger-cdn', '/openapi.json'):
        return
    expected = os.getenv('DQ_API_KEY')
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail='Invalid API key')

@asynccontextmanager
async def lifespan(app):
    init_db()
    if os.getenv('SEED_DEMO','false').lower()=='true':
        from common.seed import seed
        seed()
    yield
app=FastAPI(
    title='Financial DQ Engine API',
    description='Configurable financial DQ rules, CDEs, validation runs, reports and demo seeding.',
    version='0.3.0',
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
    openapi_url='/openapi.json',
    docs_url=None,
    swagger_ui_parameters={'docExpansion': 'list', 'displayRequestDuration': True, 'filter': True},
)

def get_db():
    with SessionLocal() as db: yield db

# Local development API key is registered at app construction so it appears
# in OpenAPI / Swagger Authorize and protects routes consistently.
# Replace with enterprise SSO/OIDC + roles before deployment.

def fail(exc):
    if isinstance(exc,LookupError):raise HTTPException(404,str(exc))
    raise HTTPException(422,str(exc))

@app.get('/docs', response_class=HTMLResponse, include_in_schema=False)
def offline_swagger_docs():
    """Fully offline, Swagger-style interactive explorer; no CDN required."""
    return render_docs()

@app.get('/swagger-cdn', include_in_schema=False)
def official_swagger_docs():
    """Official Swagger UI; requires access to swagger-ui CDN assets."""
    return get_swagger_ui_html(openapi_url=app.openapi_url, title='Financial DQ Engine — Swagger UI')

@app.get('/health', tags=['System'])
def health():return {'status':'ok'}

@app.get('/api/v1/dq/rule-types', tags=['Rule types'])
def rule_types():return [{'id':x,'name':x.replace('_',' ').title()} for x in RULE_TYPES]

@app.get('/api/v1/dq/attributes', tags=['Attributes & portfolios'])
def attributes(is_cde:str|None=None,db:Session=Depends(get_db)):
    return [vars(a) for a in source_attributes(db,is_cde.upper() if is_cde else None)]

@app.get('/api/v1/dq/attributes/{attribute_id}', tags=['Attributes & portfolios'])
def attribute(attribute_id:str,db:Session=Depends(get_db)):
    a=get_attribute(db,attribute_id)
    if not a:raise HTTPException(404,'Attribute not found')
    return vars(a)

@app.get('/api/v1/dq/attributes/{attribute_id}/rules', tags=['Attributes & portfolios'])
def attribute_rules(attribute_id:str,db:Session=Depends(get_db)):
    return [serialize_rule(db,r) for r in db.scalars(select(Rule).join(RuleMapping,Rule.id==RuleMapping.rule_id).where(RuleMapping.prj_id==attribute_id).distinct()).all()]

@app.get('/api/v1/dq/portfolios', tags=['Attributes & portfolios'])
def portfolios(db:Session=Depends(get_db)):
    return [{'short_code':p.short_code,'name':p.portfolio_name,'sector':p.sector_name,'sub_sector':p.sub_sector,'is_active':p.is_active,'document_type_id':p.document_type_id} for p in source_portfolios(db)]

@app.get('/api/v1/dq/rules', tags=['Rule management'])
def rules(db:Session=Depends(get_db)):
    return [serialize_rule(db,r) for r in db.scalars(select(Rule).order_by(Rule.id)).all()]

@app.get('/api/v1/dq/rules/{rule_id}', tags=['Rule management'])
def rule_detail(rule_id:str,db:Session=Depends(get_db)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404,'Rule not found')
    return serialize_rule(db,r)

@app.post('/api/v1/dq/rules',status_code=201, tags=['Rule management'])
def rule_create(payload:RuleInput,db:Session=Depends(get_db)):
    try:return serialize_rule(db,save_rule(db,payload))
    except (ValueError,LookupError) as e:fail(e)

@app.put('/api/v1/dq/rules/{rule_id}', tags=['Rule management'])
def rule_update(rule_id:str,payload:RuleInput,db:Session=Depends(get_db)):
    if payload.id!=rule_id:raise HTTPException(422,'ID cannot change')
    try:return serialize_rule(db,save_rule(db,payload,update=True))
    except (ValueError,LookupError) as e:fail(e)

@app.post('/api/v1/dq/rules/{rule_id}/validate', tags=['Rule management'])
def validate_rule(rule_id:str,db:Session=Depends(get_db)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404,'Rule not found')
    try:
        v=require_rule_version(db,r)
        validate_references(db,RuleInput(id=r.id,name=r.name,rule_type=r.rule_type,severity=r.severity,status='ACTIVE',targets=v.targets,portfolios=v.portfolios,config=v.config))
        return {'valid':True}
    except ValueError as e:return {'valid':False,'reason':str(e)}

@app.post('/api/v1/dq/rules/{rule_id}/activate', tags=['Rule management'])
def activate(rule_id:str,db:Session=Depends(get_db)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404,'Rule not found')
    try:
        v=require_rule_version(db,r)
        validate_references(db,RuleInput(id=r.id,name=r.name,rule_type=r.rule_type,severity=r.severity,status='ACTIVE',targets=v.targets,portfolios=v.portfolios,config=v.config))
    except ValueError as e:fail(e)
    change_rule_status(db,r,'ACTIVE',change_reason='Rule activated via API');return {'status':'ACTIVE','version':r.current_version}

@app.post('/api/v1/dq/rules/{rule_id}/deactivate', tags=['Rule management'])
def deactivate(rule_id:str,db:Session=Depends(get_db)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404,'Rule not found')
    try:change_rule_status(db,r,'INACTIVE',change_reason='Rule deactivated via API')
    except ValueError as e:fail(e)
    return {'status':'INACTIVE','version':r.current_version}

@app.get('/api/v1/dq/rules/{rule_id}/versions', tags=['Rule management'])
def versions(rule_id:str,db:Session=Depends(get_db)):
    return [serialize_history(v) for v in db.scalars(select(RuleVersion).where(RuleVersion.rule_id==rule_id).order_by(RuleVersion.version)).all()]

@app.get('/api/v1/dq/rules/{rule_id}/audit', tags=['Rule audit'])
def rule_audit(rule_id:str,db:Session=Depends(get_db)):
    """SCD Type 2 history: closed and active intervals, thresholds, editor, reason."""
    if not db.get(Rule,rule_id): raise HTTPException(404,'Rule not found')
    return {'rule_id':rule_id,'history':versions(rule_id,db)}

@app.get('/api/v1/dq/system/routes', tags=['System'])
def route_catalog():
    """Compare this with /openapi.json when the Swagger UI assets are blocked."""
    spec=app.openapi()
    return {'swagger_url':'/docs', 'openapi_url':'/openapi.json',
            'total_paths':len(spec.get('paths',{})),
            'routes':[{'path':path,'methods':sorted(method.upper() for method in operations if method.lower() in {'get','post','put','patch','delete'})}
                      for path,operations in sorted(spec.get('paths',{}).items())]}

@app.get('/api-reference', response_class=HTMLResponse, include_in_schema=False)
def api_reference():
    """CDN-free route listing for restricted corporate browser networks."""
    from html import escape
    listing=route_catalog()
    items=''.join('<li><code>'+escape(', '.join(x['methods']))+' '+escape(x['path'])+'</code></li>' for x in listing['routes'])
    return ('<!doctype html><html><head><meta charset="utf-8"><title>DQ API routes</title></head>'
            '<body><h1>Financial DQ Engine — API routes</h1><p>Swagger: <a href="/docs">/docs</a> '
            'OpenAPI JSON: <a href="/openapi.json">/openapi.json</a></p>'
            '<p>For Swagger use port 8000: <strong>http://localhost:8000/docs</strong>. '
            'If Swagger shows no endpoints, network restrictions may block its JavaScript assets.</p><ol>'+items+'</ol></body></html>')

@app.get('/api/v1/dq/rules/{rule_id}/attributes', tags=['Rule management'])
def mappings(rule_id:str,db:Session=Depends(get_db)):
    return [{'id':m.id,'prj_id':m.prj_id,'role':m.role,'collection_type':m.collection_type} for m in db.scalars(select(RuleMapping).where(RuleMapping.rule_id==rule_id)).all()]

@app.post('/api/v1/dq/executions',status_code=202, tags=['Execution & results'])
def start(payload:ExecutionInput,idempotency_key:str|None=Header(None),db:Session=Depends(get_db)):
    if idempotency_key:
        existing=db.scalar(select(Execution).where(Execution.idempotency_key==idempotency_key))
        if existing:return {'execution_id':existing.id,'status':existing.status}
    eid=str(uuid.uuid4());run=Execution(id=eid,scope=payload.model_dump(),idempotency_key=idempotency_key)
    db.add(run);db.commit()
    if os.getenv('EXECUTION_MODE','sync')=='celery':
        from worker.tasks import run_validation
        run_validation.delay(eid)
    else:execute(eid)
    db.refresh(run)
    return {'execution_id':eid,'status':run.status}

@app.get('/api/v1/dq/executions', tags=['Execution & results'])
def executions(db:Session=Depends(get_db)):
    return [{'id':x.id,'status':x.status,'scope':x.scope,'requested_at':x.requested_at.isoformat(),'error':x.error} for x in db.scalars(select(Execution).order_by(Execution.requested_at.desc()).limit(100)).all()]

@app.get('/api/v1/dq/executions/{execution_id}', tags=['Execution & results'])
def execution_detail(execution_id:str,db:Session=Depends(get_db)):
    x=db.get(Execution,execution_id)
    if not x:raise HTTPException(404,'Execution not found')
    return {'id':x.id,'status':x.status,'scope':x.scope,'error':x.error,'requested_at':x.requested_at,'completed_at':x.completed_at}

@app.get('/api/v1/dq/executions/{execution_id}/results', tags=['Execution & results'])
def results(execution_id:str,db:Session=Depends(get_db)):
    if not db.get(Execution,execution_id):raise HTTPException(404,'Execution not found')
    return summary(db,execution_id)

@app.post('/api/v1/dq/executions/{execution_id}/retry',status_code=202, tags=['Execution & results'])
def retry(execution_id:str,db:Session=Depends(get_db)):
    x=db.get(Execution,execution_id)
    if not x:raise HTTPException(404,'Execution not found')
    if x.status!='FAILED':raise HTTPException(409,'Only failed jobs can be retried')
    return start(ExecutionInput(**{k:v for k,v in x.scope.items() if k in ExecutionInput.model_fields}),None,db)

@app.get('/api/v1/dq/schedules', tags=['Scheduling'])
def schedules(db:Session=Depends(get_db)):
    return [{'id':s.id,'name':s.name,'frequency':s.frequency,'hour_utc':s.hour_utc,'minute_utc':s.minute_utc,'day_of_month':s.day_of_month,'rule_ids':s.rule_ids,'active':s.active} for s in db.scalars(select(Schedule)).all()]

@app.post('/api/v1/dq/schedules',status_code=201, tags=['Scheduling'])
def create_schedule(data:ScheduleInput,db:Session=Depends(get_db)):
    s=Schedule(**data.model_dump());db.add(s);db.commit();db.refresh(s);return {'id':s.id}

@app.put('/api/v1/dq/schedules/{schedule_id}', tags=['Scheduling'])
def update_schedule(schedule_id:int,data:ScheduleInput,db:Session=Depends(get_db)):
    s=db.get(Schedule,schedule_id)
    if not s:raise HTTPException(404,'Schedule not found')
    for k,v in data.model_dump().items():setattr(s,k,v)
    db.commit();return {'id':s.id}

@app.delete('/api/v1/dq/schedules/{schedule_id}', tags=['Scheduling'])
def delete_schedule(schedule_id:int,db:Session=Depends(get_db)):
    s=db.get(Schedule,schedule_id)
    if not s:raise HTTPException(404,'Schedule not found')
    db.delete(s);db.commit();return {'deleted':True}

@app.post('/api/v1/dq/rules/import', tags=['Rule management'])
async def import_excel(file:UploadFile=File(...),db:Session=Depends(get_db)):
    """Preview bank/insurance Excel without modifying rule or master tables."""
    blob=await file.read(MAX_SIZE+1)
    if len(blob)>MAX_SIZE:raise HTTPException(413,'Workbook too large (30 MB maximum)')
    try:candidates=build_candidates(db,blob,file.filename or 'rules.xlsx')
    except (ValueError,UnicodeError,ImportError) as exc:raise HTTPException(422,str(exc))
    batch=ImportBatch(id=str(uuid.uuid4()),filename=file.filename or 'rules.xlsx',candidates=candidates)
    db.add(batch);db.commit()
    return {'batch_id':batch.id,'rows':len(candidates),'candidates':candidates[:250],
        'duplicates':sum(c['status']=='DUPLICATE_EXISTING' for c in candidates),
        'requires_cde_approval':sum(c['needs_cde_update'] for c in candidates),
        'note':'Preview only. Confirm separately to create DRAFT rules and optionally promote approved CDEs.'}

@app.post('/api/v1/dq/rules/import/{batch_id}/confirm', tags=['Rule management'])
def confirm_import(batch_id:str, approve_cde_updates:bool=False, changed_by:str='api-user',db:Session=Depends(get_db)):
    """Idempotent approval with explicit opt-in for modifying the existing master."""
    batch=db.get(ImportBatch,batch_id)
    if not batch:raise HTTPException(404,'Import batch not found')
    if approve_cde_updates and os.getenv('DQ_ALLOW_CDE_PROMOTION','false').lower() not in ('true','1','yes'):
        raise HTTPException(403,'CDE promotion disabled: set DQ_ALLOW_CDE_PROMOTION=true only with approved database permissions')
    from common.reference_data import portfolios as portfolio_rows
    from common.services import save_rule
    codes=portfolio_rows(db)
    created=[];duplicates=[];skipped=[];cde_changes=[]
    # Revisit rows that were not created; repeated confirmation never duplicates rules.
    for c in batch.candidates:
        if c.get('event')=='IMPORT_CONFIRM': continue
        rid=c['rule_id']
        if db.get(Rule,rid):
            duplicates.append(rid);continue
        if c['rule_type']=='MANUAL_REVIEW' or not get_attribute(db,c['prj_id']):
            skipped.append({'rule_id':rid,'reason':'Manual review or unknown PRJ ID'});continue
        group=c['portfolio_group']
        applicable=[x.short_code for x in codes if x.is_active and (group=='ALL' or
            group.lower() in (x.short_code+' '+x.portfolio_name+' '+(x.sector_name or '')).lower())]
        if group!='ALL' and not applicable:
            skipped.append({'rule_id':rid,'reason':'Portfolio group not mapped to active reference'});continue
        attr=get_attribute(db,c['prj_id'])
        if attr.is_cde!='Y':
            if not approve_cde_updates:
                skipped.append({'rule_id':rid,'reason':'CDE update approval required'});continue
            try: cde_changes.append(promote_cde(db,c['prj_id']))
            except Exception as exc:
                db.rollback()
                raise HTTPException(403,'Cannot update prj_attribute_master: '+type(exc).__name__) from exc
        config=dict(c['config']);config['collection_type']=c['collection_type'];config['require_cde']=True
        try:
            payload=RuleInput(id=rid,name=(c['attribute_name']+' — '+c['description'])[:200],
                rule_type=c['rule_type'],targets=[c['prj_id']],portfolios=applicable,
                config=config,status='DRAFT',changed_by=changed_by)
            save_rule(db,payload)
            created.append(rid)
        except (ValueError,LookupError) as exc:
            db.rollback()
            skipped.append({'rule_id':rid,'reason':str(exc)})
    # Persist audit of confirmed changes within existing import batch JSON (no new table).
    payload=list(batch.candidates)
    payload.append({'event':'IMPORT_CONFIRM','actor':changed_by,'created_rule_ids':created,
        'duplicate_rule_ids':duplicates,'cde_changes':cde_changes,'skipped':skipped,
        'timestamp':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    batch.candidates=payload
    db.commit()
    return {'created':len(created),'duplicates':len(duplicates),'skipped':skipped,
        'cde_updates':cde_changes,'note':'Created rules are DRAFT; activate after review.'}

@app.get('/api/v1/dq/rules/import/{batch_id}', tags=['Rule management'])
def import_detail(batch_id:str,db:Session=Depends(get_db)):
    b=db.get(ImportBatch,batch_id)
    if not b:raise HTTPException(404,'Import not found')
    return {'batch_id':b.id,'filename':b.filename,'candidates':b.candidates}

@app.get('/api/v1/dq/reports/{execution_id}', tags=['Reporting'])
def report(execution_id:str,db:Session=Depends(get_db)):
    if not db.get(Execution,execution_id):raise HTTPException(404,'Execution not found')
    return summary(db,execution_id)

@app.get('/api/v1/dq/reports/{execution_id}/download', tags=['Reporting'])
def download(execution_id:str,db:Session=Depends(get_db)):
    if not db.get(Execution,execution_id):raise HTTPException(404,'Execution not found')
    return StreamingResponse(io.BytesIO(excel_report(db,execution_id)),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="dq_{execution_id}.xlsx"'})

@app.post('/api/v1/dq/reports/{execution_id}/email', tags=['Reporting'])
def email_report(execution_id:str,recipient:str,db:Session=Depends(get_db)):
    if not db.get(Execution,execution_id):raise HTTPException(404,'Execution not found')
    host=os.getenv('SMTP_HOST')
    if not host:raise HTTPException(503,'SMTP not configured')
    msg=EmailMessage();msg['From']=os.getenv('SMTP_FROM','dq@example.invalid');msg['To']=recipient;msg['Subject']='Financial DQ report'
    data=summary(db,execution_id);msg.set_content(f'DQ run {execution_id}\nTotal: {data["total"]}\nOutcomes: {data["outcomes"]}')
    msg.add_attachment(excel_report(db,execution_id),maintype='application',subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename='DQ_Report.xlsx')
    try:
        with smtplib.SMTP(host,int(os.getenv('SMTP_PORT','587')),timeout=15) as conn:
            if os.getenv('SMTP_TLS','true')=='true':conn.starttls()
            if os.getenv('SMTP_USER'):conn.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD'])
            conn.send_message(msg)
        db.add(Notification(execution_id=execution_id,recipient=recipient,status='SENT'));db.commit()
    except Exception as e:
        db.add(Notification(execution_id=execution_id,recipient=recipient,status='FAILED',reason=type(e).__name__));db.commit()
        raise HTTPException(502,'Email delivery failed')
    return {'status':'SENT'}

@app.get('/api/v1/dq/dashboard', tags=['Dashboard'])
def dashboard(db:Session=Depends(get_db)):
    return {'rules':db.scalar(select(func.count()).select_from(Rule)), 'active_rules':db.scalar(select(func.count()).select_from(Rule).where(Rule.status=='ACTIVE')),'executions':db.scalar(select(func.count()).select_from(Execution)),'violations':db.scalar(select(func.count()).select_from(Result).where(Result.outcome=='FAIL'))}


@app.get('/api/v1/dq/demo/status', tags=['Demo'])
def demo_status(db: Session = Depends(get_db)):
    """Read-only visibility into demo readiness; no data modifications."""
    from common.reference_data import attributes, portfolios
    attrs = attributes(db, 'Y', limit=3)
    active_ports = [p for p in portfolios(db) if p.is_active]
    facts = db.scalar(select(func.count()).select_from(FinancialFact).where(FinancialFact.document_id == 'DEMO-001'))
    return {
        'enabled': os.getenv('SEED_DEMO', 'false').lower() == 'true',
        'eligible_cdes': [a.prj_id for a in attrs],
        'active_portfolios': [p.short_code for p in active_ports[:10]],
        'demo_fact_count': facts,
        'demo_rules': [r for r in ('Rule_06', 'Rule_09') if db.get(Rule, r)],
        'notes': 'Demo uses existing read-only PRJ masters; only DQ-owned and demo facts tables are written.',
    }


@app.post('/api/v1/dq/demo/seed', tags=['Demo'])
def seed_demo_data():
    """Explicit, idempotent, DEVELOPMENT-ONLY seed; no schema DDL or master writes."""
    if os.getenv('SEED_DEMO', 'false').lower() != 'true':
        raise HTTPException(status_code=403, detail='Demo seeding disabled. Set SEED_DEMO=true in a development environment.')
    from common.seed import seed
    return seed()

@app.get('/api/v1/dq/sources', tags=['Execution & results'])
def available_sources():
    return [{'id':'DEMO_NORMALIZED','name':'Demo normalized financial facts'},
            {'id':'UNS_FINANCIAL_LINE_ITEMS','name':'uns_financial_line_items (mapped read-only)'}]
