import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class DeliveryRpcTests(unittest.TestCase):
    def test_real_stdio_lists_delivery_and_rejects_invalid_job_without_host_access(self):
        messages=[{'id':1,'method':'initialize','params':{}},
                  {'id':2,'method':'tools/list'},
                  {'id':3,'method':'tools/call','params':{'name':'deliver_repair','arguments':{'mode':'run','job_id':'../bad','plan_hash':'x'}}}]
        p=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'serve.py')],
            input='\n'.join(json.dumps(x) for x in messages)+'\n',capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)
        rows=[json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in rows],[1,2,3])
        self.assertEqual(rows[0]['result']['serverInfo']['version'],'0.3.1')
        self.assertIn('deliver_repair',[t['name'] for t in rows[1]['result']['tools']])
        self.assertTrue(rows[2]['result']['isError'])
        self.assertEqual(json.loads(rows[2]['result']['content'][0]['text'])['error']['code'],'invalid_request')

    def test_real_cli_has_json_only_failure_for_invalid_delivery(self):
        p=subprocess.run([sys.executable,'-X','utf8','-m','bugfixer','deliver'],cwd=ROOT,
            input=json.dumps({'mode':'run','job_id':'bad/path','plan_hash':'x'}),capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(json.loads(p.stdout)['error']['code'],'invalid_request')
        self.assertNotIn('Traceback',p.stderr)
