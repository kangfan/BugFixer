"""Read deployed ZenTao forms only; never submit a business form."""
import json
from pathlib import Path
from html.parser import HTMLParser
from bugfixer.credentials import resolve
from bugfixer.http import Client
from bugfixer.zentao import fetch, unwrap, BASE
from bugfixer.service import DEFAULT_DATABASE

class Forms(HTMLParser):
    def __init__(self):
        super().__init__(); self.controls=[]; self.current=None
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='form': self.controls.append({'tag':tag, **a})
        if tag in ('input','select','textarea'):
            self.current={'tag':tag, **a}; self.controls.append(self.current)
        if tag=='option': self.controls.append({'tag':tag, **a})
    def handle_data(self, text):
        if self.controls and text.strip():
            self.controls[-1]['text']=(self.controls[-1].get('text','')+text)[:180]

root=Path(__file__).resolve().parent/'discovery'/'zentao-write';root.mkdir(parents=True,exist_ok=True)
credential=resolve({'source':'zentao','credential_ref':'zentao'},DEFAULT_DATABASE)
client=Client()
context=fetch(client,{'id':'263856'},credential)['context']
summary={'bug_id':'263856','status':context['status'],'assigned_to':context['assigned_to'],'forms':{}}
for action in ('edit','resolve'):
    page=client.get(BASE+f'/bug-{action}-263856.html').body.decode('utf-8')
    parser=Forms();parser.feed(page)
    (root/f'{action}.html').write_text(page,encoding='utf-8')
    # Exclude original textarea contents and any hidden challenges from console.
    controls=[{k:v for k,v in x.items() if k!='text' and not(k=='value' and x.get('type')=='hidden')} for x in parser.controls]
    summary['forms'][action]=controls
    data=unwrap(client.get(BASE+f'/bug-{action}-263856.json'))
    (root/f'{action}.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    summary['forms'][action+'_json_keys']=list(data)
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':'read_only_discovery_complete','summary_path':str(root/'summary.json'),
                  'json_keys':{action:summary['forms'][action+'_json_keys'] for action in ('edit','resolve')}}))
