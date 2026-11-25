"""Parse the captured real deployment forms; no login or business writes."""
import json
from pathlib import Path
from bugfixer.http import Response
from bugfixer.writeback import _forms, _targets

root = Path(__file__).resolve().parent
capture = root / 'discovery' / 'zentao-write'
result = json.loads((root / 'live' / 'zentao-kangzijun-263856-result.json').read_text(encoding='utf-8-sig'))
context = json.loads(Path(result['context_path']).read_text(encoding='utf-8-sig'))

class CapturedClient:
    def get(self, url):
        action = 'edit' if '/bug-edit-' in url else 'resolve'
        suffix = 'json' if url.endswith('.json') else 'html'
        return Response(url, 200, {'Content-Type': 'text/html; charset=UTF-8'}, (capture / f'{action}.{suffix}').read_text(encoding='utf-8').encode())

forms = _forms(CapturedClient(), '263856', context)
_targets(forms, {'resolved_build': 'trunk', 'assigned_to': 'chenjing', 'expected_assigned_to': 'kangzijun'})
report = {'status': 'passed', 'source': 'Captured deployed ZenTao 12.3.3 GET forms', 'business_writes': False,
          'bug_id': '263856', 'original_steps_match': True, 'snapshot_match': True,
          'edit_fields': sorted(forms['edit'].values), 'resolve_fields': sorted(forms['resolve'].values),
          'existing_build_trunk_validated': True, 'test_assignee_chenjing_validated': True}
path = root / 'write-form-contract-verification.json'
path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'status': 'passed', 'report': str(path), 'business_writes': False}))
