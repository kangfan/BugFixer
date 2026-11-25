"""Authorized existing ticket attachments; no login or ticket mutations."""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bugfixer.artifacts import ArtifactStore, write_json
from bugfixer.common import BugFixerError
from bugfixer.http import Client

root=Path(__file__).resolve().parent/'live'
root.mkdir(exist_ok=True)
run=root/('download-smoke-'+uuid.uuid4().hex[:10]);run.mkdir()
store=ArtifactStore(root)
client=Client(timeout=20,retries=1)
wx=Path('G:/okr/outputs/analysis-wx-sign2223144-20261124')
overseas=Path('G:/okr/outputs/analysis-loading319220-20261123')
sources=[
 {'url':json.loads((wx/'log-evidence-redacted.json').read_text(encoding='utf-8-sig'))['sourceUrl'],'name':'ticket2223144-log','kind':'log','expected_type':'text'},
 {'url':json.loads((overseas/'ticket-observed.json').read_text(encoding='utf-8-sig'))['sourceLogLinks'][0],'name':'ticket319220-first-log','kind':'log','expected_type':'zip','expected_sha256':'b649335472d736ff2a0c502a66ace804272b0f77bb26a5c45dc048b468ae319f'},
 {'url':json.loads((wx/'acquisition.json').read_text(encoding='utf-8-sig'))['video']['sourceUrl'],'name':'ticket2223144-video','kind':'video','expected_type':'mp4','expected_sha256':'b76c1cdfdafb413a2451d6be4a3d114cdda8ce1c41116e09283adaa9ec755447'},
]
results=[]
for i,source in enumerate(sources):
 try:
  result=store.download(client,source,run,i)
  result['accepted']=result['detected_type']==source['expected_type'] and (not source.get('expected_sha256') or result['sha256']==source['expected_sha256'])
 except BugFixerError as e:
  result={'name':source['name'],'accepted':False,'error':{'code':e.code,'message':e.message}}
 results.append(result)
 print(json.dumps({'name':source['name'],'accepted':result['accepted'],'type':result.get('detected_type'),'bytes':result.get('size_bytes'),'sha256':result.get('sha256'),'error':result.get('error')},ensure_ascii=False),flush=True)
if results[-1].get('accepted'):
 repeated=store.download(client,sources[-1],run,3)
 results.append({'name':'conditional-cache-video','accepted':repeated['cache_reused'],'conditional_not_modified':repeated['conditional_not_modified']})
report={'scope':'Live original attachment GETs from previously matched/authorized tickets, not authenticated API-context acceptance','results':results,'all_passed':all(x['accepted'] for x in results),'ticket_or_business_mutations':False}
write_json(run/'report.json',report)
(root/'latest-download-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'all_passed':report['all_passed'],'report':str(run/'report.json')}))
