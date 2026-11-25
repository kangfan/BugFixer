"""Verify authorized six-Bug delivery receipts from disk; no credentials or HTTP calls."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from bugfixer.writeback import _done

root = Path(__file__).resolve().parents[1]
delivery = Path('G:/okr/outputs/oversea71-tour-20261124')
commits = json.loads((delivery/'commits.json').read_text(encoding='utf-8-sig'))
checks = []
with sqlite3.connect((root/'data/bugfixer.sqlite3').as_uri()+'?mode=ro', uri=True) as conn:
    for entry in commits:
        bug_id = entry['bugId']
        filename = 'writeback-reconciled-263761.json' if bug_id == '263761' else f'writeback-result-{bug_id}.json'
        receipt = json.loads((delivery/filename).read_text(encoding='utf-8-sig'))
        assert receipt['status'] == 'completed' and receipt['verified'] is True
        row = conn.execute('SELECT state,plan_json FROM writeback_requests WHERE request_id=?', (receipt['request_id'],)).fetchone()
        assert row[0] == 'completed'
        plan = json.loads(row[1])
        context = json.loads(Path(receipt['readback_path']).read_text(encoding='utf-8-sig'))
        assert _done(context, plan)
        assert context['id'] == bug_id and context['assigned_to'] == entry['creator']
        assert plan['request']['commit'] == entry['commit'] and entry['remoteVerified'] is True
        assert plan['request']['resolved_build'] == 'trunk'
        checks.append({'id':bug_id, 'status':'resolved', 'resolution':'fixed', 'assigned_to':entry['creator'],
                       'resolved_build':'trunk', 'commit':entry['commit'], 'remote_verified':True,
                       'description_comment_and_original_fields_verified':True,
                       'readback_path':receipt['readback_path'], 'reconciled':receipt.get('reconciled',False)})
assert len(checks) == 6
report = {'verified_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
          'scope':'Six specifically owner-approved overseas7.1 repairs; actual service POSTs by maintainer, checked saved readbacks.',
          'approval_ids':['tour71-layout-20261124-01','tour71-resolvedbuild-20261124-01'],
          'business_writes_this_verifier':False, 'checks':checks,
          'first_write_reconciled_without_reposting':True}
path = root/'acceptance/live/writeback-six-tour-bugs-20261124.json'
path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'status':'passed','verified_real_writebacks':len(checks),'report':str(path)}))
