"""Streamlit UI entry point. Works both with Poetry and direct Streamlit execution.

Streamlit starts a script with its own directory on sys.path, so the sibling
`common` package must be resolved explicitly from the repository root.
"""
import os
import sys
from pathlib import Path

# Important: do this before importing common.* or any Streamlit-specific code.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import common.config  # Loads PROJECT_ROOT/.env without overriding OS env vars.
import requests
import streamlit as st
import pandas as pd

API=os.getenv('API_URL','http://localhost:8000')
HEADERS={'X-API-Key':os.getenv('DQ_API_KEY','')}
st.set_page_config(page_title='Financial DQ Control Center',layout='wide',page_icon='📊')
st.title('Financial Data Quality | Control Center')
st.caption('Rule configuration • CDE registry • Orchestration • Execution results')

def call(method,path,**kwargs):
    try:
        resp=requests.request(method,API+path,headers=HEADERS,timeout=45,**kwargs)
        if not resp.ok:
            st.error(f'API {resp.status_code}: {resp.text[:500]}');return None
        return resp.json()
    except requests.RequestException as e:
        st.error(f'API connection error: {e}');return None

pages=['Dashboard','CDE & Portfolio Registry','Rule Registry','Rule Builder','Rule Orchestrator','Execution History','DQ Reports','Excel Rule Import','Schedules']
page=st.sidebar.radio('Navigation',pages)
st.sidebar.caption(f'API: {API}')
st.sidebar.markdown(f'[Swagger API documentation]({API}/docs) · [Route catalogue]({API}/api-reference)')

if page=='Dashboard':
    st.caption('Demo seed is available only when SEED_DEMO=true. It never changes the existing PRJ master tables.')
    demo=call('GET','/api/v1/dq/demo/status')
    if demo:
        st.write('Demo records:', demo.get('demo_fact_count', 0), '| Eligible CDEs:', len(demo.get('eligible_cdes', [])))
        if demo.get('enabled') and st.button('Seed sample DQ records'):
            seeded=call('POST','/api/v1/dq/demo/seed')
            if seeded: st.success(str(seeded));st.rerun()
    data=call('GET','/api/v1/dq/dashboard')
    if data:
        cols=st.columns(4)
        for col,(k,v) in zip(cols,data.items()):col.metric(k.replace('_',' ').title(),v)
    st.subheader('Latest executions')
    runs=call('GET','/api/v1/dq/executions') or []
    if runs:st.dataframe(pd.DataFrame(runs),use_container_width=True)
elif page=='CDE & Portfolio Registry':
    cde=st.checkbox('Show only CDEs',value=True)
    rows=call('GET','/api/v1/dq/attributes'+('?is_cde=Y' if cde else ''))
    st.subheader('Attribute master')
    if rows is not None:st.dataframe(pd.DataFrame(rows),use_container_width=True)
    st.subheader('Portfolio reference')
    rows=call('GET','/api/v1/dq/portfolios')
    if rows is not None:st.dataframe(pd.DataFrame(rows),use_container_width=True)
elif page=='Rule Registry':
    rules=call('GET','/api/v1/dq/rules') or []
    if rules:
        st.dataframe(pd.DataFrame([{k:v for k,v in r.items() if k!='config'} for r in rules]),use_container_width=True)
        rid=st.selectbox('Select rule', [r['id'] for r in rules])
        rule=next(r for r in rules if r['id']==rid)
        st.json(rule)
        if rule.get('configuration_error') == 'MISSING_RULE_VERSION':
            st.warning('This rule has no matching dq_rule_versions record. Edit and save the rule in Rule Builder to create a new configuration snapshot, or ask your DBA to restore the historical version from backup. Its current configuration cannot be inferred automatically.')
        x,y=st.columns(2)
        if x.button('Activate'):call('POST',f'/api/v1/dq/rules/{rid}/activate');st.rerun()
        if y.button('Deactivate'):call('POST',f'/api/v1/dq/rules/{rid}/deactivate');st.rerun()
        versions=call('GET',f'/api/v1/dq/rules/{rid}/versions')
        with st.expander('SCD Type 2 audit history'):
            audit=call('GET',f'/api/v1/dq/rules/{rid}/audit')
            if audit:
                st.dataframe(pd.DataFrame(audit['history']),use_container_width=True)
                st.json(audit)
    else:st.info('No rules configured yet')
elif page=='Rule Builder':
    st.subheader('Create or update a validation rule')
    attrs=call('GET','/api/v1/dq/attributes') or []
    ports=call('GET','/api/v1/dq/portfolios') or []
    rules=call('GET','/api/v1/dq/rules') or []
    existing=st.selectbox('Edit existing rule (optional)',['Create new']+[r['id'] for r in rules])
    old=next((r for r in rules if r['id']==existing),{})
    with st.form('rule_form'):
        rid=st.text_input('Rule ID',value=old.get('id','Rule_10'),disabled=bool(old))
        name=st.text_input('Name',value=old.get('name','New financial validation'))
        types=call('GET','/api/v1/dq/rule-types') or []
        options=[x['id'] for x in types]
        old_type=old.get('rule_type','LIMIT')
        typ=st.selectbox('Rule type',options,index=options.index(old_type) if old_type in options else 0)
        all_ids=[x['prj_id'] for x in attrs]
        targets=st.multiselect('Target PRJ IDs',all_ids,default=old.get('targets',[]))
        port_codes=[x['short_code'] for x in ports]
        portfolio=st.multiselect('Applicable portfolios (empty = all)',port_codes,default=old.get('portfolios',[]))
        severity=st.selectbox('Severity',['LOW','MEDIUM','HIGH','CRITICAL'],index=['LOW','MEDIUM','HIGH','CRITICAL'].index(old.get('severity','MEDIUM')))
        status=st.selectbox('Status',['DRAFT','ACTIVE'],index=0 if old.get('status','DRAFT')!='ACTIVE' else 1)
        default_cfg=old.get('config',{'operator':'>=','threshold':'7','require_cde':True})
        old_type_threshold=old.get('threshold_type') or default_cfg.get('threshold_type') or ('PERCENTAGE' if typ=='MOVEMENT' else 'COUNT' if typ=='DUPLICATE' else 'AMOUNT')
        if old_type_threshold not in ('PERCENTAGE','COUNT','AMOUNT'): old_type_threshold='AMOUNT'
        threshold_type=st.selectbox('Threshold Type',['PERCENTAGE','COUNT','AMOUNT'], index=['PERCENTAGE','COUNT','AMOUNT'].index(old_type_threshold),
            help='PERCENTAGE: 1.1 means 1.1%, COUNT: number of matching records, AMOUNT: numeric financial value')
        threshold_value=st.text_input('Threshold Value',value=str(old.get('threshold_value') or default_cfg.get('threshold_value') or default_cfg.get('threshold') or '1.1'),
            help='Examples: 1 or 1.1. MOVEMENT treats the operator as a BREACH; LIMIT treats it as a PASS criterion.')
        changed_by=st.text_input('Changed by',value='ui-user')
        change_reason=st.text_input('Change reason',value='Rule configuration updated')
        st.caption('Configurable parameters; MOVEMENT operators denote a breach, LIMIT operators denote a passing condition.')
        cfg_text=st.text_area('Rule configuration (JSON)',value=__import__('json').dumps(default_cfg,indent=2),height=180)
        submit=st.form_submit_button('Save rule')
    if submit:
        try:
            cfg=__import__('json').loads(cfg_text)
            if typ in ('LIMIT','MOVEMENT','DUPLICATE'):
                cfg.update(threshold_value=threshold_value,threshold_type=threshold_type,threshold=threshold_value)
            payload={'id':rid,'name':name,'rule_type':typ,'targets':targets,'portfolios':portfolio,'severity':severity,
                'status':status,'config':cfg,'changed_by':changed_by,'change_reason':change_reason}
            response=call('PUT' if old else 'POST',f'/api/v1/dq/rules/{rid}' if old else '/api/v1/dq/rules',json=payload)
            if response:st.success(f'Saved rule version {response["version"]}')
        except Exception as e:st.error(f'Invalid configuration: {e}')
elif page=='Rule Orchestrator':
    rules=call('GET','/api/v1/dq/rules') or []
    ports=call('GET','/api/v1/dq/portfolios') or []
    ids=st.multiselect('Rules (empty = all active)',[r['id'] for r in rules])
    source=st.selectbox('Financial data source',['DEMO_NORMALIZED','UNS_FINANCIAL_LINE_ITEMS'])
    cde_only=st.checkbox('CDE-driven execution (requires selected portfolio)',value=False)
    port=st.selectbox('Portfolio',['All']+[p['short_code'] for p in ports])
    company=st.text_input('Company filter (optional)')
    period=st.text_input('Reporting period (optional, e.g. 2026-12)')
    document=st.text_input('Document ID (optional)')
    if st.button('Run validations',type='primary'):
        if cde_only and port=='All':st.error('Select a portfolio for CDE-driven execution.');st.stop()
        payload={'rule_ids':ids,'source':source,'cde_only':cde_only,'portfolio':None if port=='All' else port,'company':company or None,'period':period or None,'document_id':document or None}
        response=call('POST','/api/v1/dq/executions',json=payload)
        if response:st.success(f'Run {response["execution_id"]} — {response["status"]}');st.session_state['run_id']=response['execution_id']
elif page=='Execution History':
    runs=call('GET','/api/v1/dq/executions') or []
    if runs:
        st.dataframe(pd.DataFrame(runs),use_container_width=True)
        eid=st.selectbox('Inspect execution',[r['id'] for r in runs],index=0)
        result=call('GET',f'/api/v1/dq/executions/{eid}/results')
        if result:
            st.write('Outcomes:',result['outcomes'])
            st.dataframe(pd.DataFrame(result['results']),use_container_width=True)
    else:st.info('Run a validation to see results.')
elif page=='DQ Reports':
    runs=call('GET','/api/v1/dq/executions') or []
    if runs:
        eid=st.selectbox('Execution',[r['id'] for r in runs])
        report=call('GET',f'/api/v1/dq/reports/{eid}')
        if report:
            st.metric('Validations',report['total'])
            st.json(report['outcomes'])
            st.dataframe(pd.DataFrame(report['results']),use_container_width=True)
        try:
            resp=requests.get(f'{API}/api/v1/dq/reports/{eid}/download',headers=HEADERS,timeout=30)
            if resp.ok:st.download_button('Download Excel',resp.content,file_name=f'dq_{eid}.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except requests.RequestException:pass
        with st.form('email'):
            to=st.text_input('Email recipient')
            if st.form_submit_button('Email Excel report') and to:
                if call('POST',f'/api/v1/dq/reports/{eid}/email',params={'recipient':to}):st.success('Email sent')
    else:st.info('No reports yet')
elif page=='Excel Rule Import':
    st.info('Upload previews do not modify master data. Confirming creates DRAFT rules; promoting non-CDEs requires explicit approval and database UPDATE privilege.')
    file=st.file_uploader('Upload rule workbook',type=['xlsx','xls','csv','prj'])
    if file and st.button('Validate and preview import'):
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        result=call('POST','/api/v1/dq/rules/import',files={'file':(file.name,file.getvalue(),mime)})
        if result:
            st.session_state['import_batch_id']=result['batch_id']
            st.success(f'Batch {result["batch_id"]} — {result["rows"]} mappings; {result["duplicates"]} already exist')
            st.dataframe(pd.DataFrame(result['candidates']),use_container_width=True)
    batch_id=st.session_state.get('import_batch_id')
    if batch_id:
        st.caption(f'Current import batch: {batch_id}')
        approved=st.checkbox('I authorize promotion of imported PRJ IDs with is_cde=false/blank to TRUE in prj_attribute_master (requires UPDATE permission)')
        actor=st.text_input('Approved by',value='ui-user')
        if st.button('Confirm import and create draft rules'):
            result=call('POST',f'/api/v1/dq/rules/import/{batch_id}/confirm',params={'approve_cde_updates':str(approved).lower(),'changed_by':actor})
            if result: st.json(result)
elif page=='Schedules':
    rows=call('GET','/api/v1/dq/schedules') or []
    if rows:st.dataframe(pd.DataFrame(rows),use_container_width=True)
    st.caption('Schedules are dispatched by the separate Celery Beat worker in Docker mode, using UTC.')
    rules=call('GET','/api/v1/dq/rules') or []
    with st.form('schedule'):
        name=st.text_input('Schedule name',value='Daily DQ check')
        freq=st.selectbox('Frequency',['DAILY','MONTHLY'])
        hour=st.number_input('UTC hour',0,23,0)
        minute=st.number_input('UTC minute',0,59,0)
        day=st.number_input('Day of month (monthly)',1,28,1)
        ids=st.multiselect('Rule selection (empty = all active)',[r['id'] for r in rules])
        if st.form_submit_button('Create schedule'):
            result=call('POST','/api/v1/dq/schedules',json={'name':name,'frequency':freq,'hour_utc':int(hour),'minute_utc':int(minute),'day_of_month':int(day),'rule_ids':ids})
            if result:st.success(f'Schedule {result["id"]} created')
