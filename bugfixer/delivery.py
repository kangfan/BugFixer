"""One-call ordinary repair delivery; authority is read from the existing host store."""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import writeback
from . import __version__
from .artifacts import now, write_json
from .common import BugFixerError
from .credentials import connect
from .delivery_git import Git
from .delivery_host import Robot, workspace, WORKSPACE_DB
from .http import Client
from .service import DEFAULT_DATABASE, DEFAULT_OUTPUT


@contextmanager
def job_lock(database, ident):
    path = Path(database).parent/'.delivery-locks'/ident
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BugFixerError('job_busy', 'A process already owns this delivery; no concurrent run started.') from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__('os').name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate(m):
    required = {'task_mode', 'repo', 'branch', 'credential_ref', 'approval_ids', 'notification_mode', 'validation', 'items'}
    if not isinstance(m, dict) or not required <= set(m) or set(m) - required - {'source_dispatch_id'} or m['task_mode'] != 'repair':
        raise BugFixerError('invalid_manifest', 'Supply the documented repair manifest; parameters never grant approval.')
    if (not all(isinstance(m[k], str) and 0 < len(m[k]) < 1000 for k in ('repo', 'branch', 'credential_ref'))
            or m['notification_mode'] not in ('caller_and_creator', 'creator_in_group', 'group_only')
            or not isinstance(m['approval_ids'], list) or len(m['approval_ids']) > 40
            or any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', x) for x in m['approval_ids'])):
        raise BugFixerError('invalid_manifest', 'Invalid project, credential, approval references or notification mode.')
    v = m['validation']
    if not isinstance(v, dict) or v.get('mode') not in ('waived', 'verified') or set(v) - {'mode', 'evidence'}:
        raise BugFixerError('invalid_manifest', 'Validation must reference a real result or an explicit scoped waiver.')
    if not isinstance(m['items'], list) or not 1 <= len(m['items']) <= 30:
        raise BugFixerError('invalid_manifest', 'A batch contains 1 to 30 explicit Bugs.')
    ids = []
    for item in m['items']:
        if not isinstance(item, dict) or set(item) != {'id', 'files', 'reason', 'change', 'acceptance', 'resolved_build'}:
            raise BugFixerError('invalid_manifest', 'Each Bug requires id/files/reason/change/acceptance/resolved_build.')
        if not isinstance(item['id'], str) or not re.fullmatch(r'[1-9][0-9]{0,11}', item['id']):
            raise BugFixerError('invalid_manifest', 'Use exact numeric Bug IDs.')
        if (not isinstance(item['files'], list) or not item['files'] or len(item['files']) > 30
                or any(not isinstance(f, str) for f in item['files'])
                or any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 2000 for k in ('reason', 'change', 'acceptance'))
                or not isinstance(item['resolved_build'], str)):
            raise BugFixerError('invalid_manifest', 'Invalid selected files, report fields or build value.')
        ids.append(item['id'])
    if len(ids) != len(set(ids)):
        raise BugFixerError('invalid_manifest', 'Duplicate Bug IDs are not allowed.')
    if 'source_dispatch_id' in m and (not isinstance(m['source_dispatch_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', m['source_dispatch_id'])):
        raise BugFixerError('invalid_manifest', 'source_dispatch_id must reference a local verified dispatch record.')
    return m


def scope(m):
    return {'bugIds': sorted(i['id'] for i in m['items']), 'project': str(Path(m['repo']).resolve()),
            'branch': m['branch'], 'files': sorted({f for i in m['items'] for f in i['files']})}


class Pipeline:
    def __init__(self, *, database=DEFAULT_DATABASE, output_root=DEFAULT_OUTPUT, workspace_db=WORKSPACE_DB,
                 host=None, robot=None, git_factory=Git, client=None):
        self.database, self.output = Path(database), Path(output_root)
        self.host = host or workspace(workspace_db)
        self.owns_host = host is None
        self.robot_override, self.git_factory, self.client = robot, git_factory, client or Client()

    def close(self):
        if self.owns_host:
            self.host.close()

    def config(self):
        def get(key):
            r = self.host.config(key)
            return r['payload'] if r else {}
        runtime, owner, ding = get('runtime'), get('owner'), get('dingTalk')
        if runtime.get('maintenanceEnabled') is not True:
            raise BugFixerError('maintenance_disabled', 'Maintenance is disabled; no delivery will execute.')
        if not owner.get('zenTaoAccount') or not all(ding.get(k) for k in ('ownerStaffId', 'allowedGroupId', 'profile', 'robotCode')):
            raise BugFixerError('host_configuration_missing', 'The host must configure the repair account and original DWS robot/group.')
        self.owner, self.ding = owner, ding
        self.robot = self.robot_override or Robot(ding)

    def db(self):
        return connect(self.database)

    def read(self, ident):
        with self.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS delivery_jobs (
                id TEXT PRIMARY KEY, scope_hash TEXT NOT NULL, plan TEXT NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL, busy INTEGER NOT NULL, updated_at TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS delivery_approval_uses (
                approval_id TEXT NOT NULL,bug_id TEXT NOT NULL,job_id TEXT NOT NULL,
                PRIMARY KEY(approval_id,bug_id))''')
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM delivery_jobs WHERE id=?', (ident,)).fetchone()
            return dict(row) if row else None

    def checkpoint(self, ident, state, data):
        with self.db() as c:
            c.execute('UPDATE delivery_jobs SET state=?,data=?,updated_at=? WHERE id=?',
                      (state, json.dumps(data, ensure_ascii=False), now(), ident))

    def issue(self, code, bug=None, **detail):
        return {'code': code, **({'id': bug} if bug else {}), **detail}

    def preflight(self, ident, manifest, *, completed=(), authority_only=False):
        self.config()
        source_verified = False; source = {}
        if manifest.get('source_dispatch_id'):
            record = self.host.records('documents','dingTalkDispatch',manifest['source_dispatch_id'])
            source = record['payload'] if record else {}
            desktop = self.host.config('desktopDispatch')
            target = desktop['payload'].get('zentaoThreadId') if desktop else None
            if (source.get('requestMode')=='analysis_only' or any(re.search(r'(?:^|\s)分析问题\s*[:：]',m.get('text','')) for m in source.get('verifiedMessages',[]))):
                raise BugFixerError('analysis_only','The verified source is analysis-only; no repair or repair-approval request is allowed.')
            if (not target or source.get('targetThreadId')!=target or source.get('desktopReceiverThreadId')!=target
                    or source.get('route')!='zentao_maintainer' or source.get('maintenanceEnabled') is not True
                    or source.get('status') not in ('desktop_received','running','waiting_input') or not source.get('verifiedMessages')):
                raise BugFixerError('source_not_verified','The host must first receive and verify this dispatch in the configured desktop chat.')
            for message in source['verifiedMessages']:
                owner = message.get('senderStaffId')==self.ding['ownerStaffId']
                group = message.get('conversationType')=='2' and message.get('conversationId')==self.ding['allowedGroupId']
                direct = message.get('conversationType')=='1' and owner
                if (not message.get('messageId') or not message.get('senderStaffId') or message.get('canRequestMaintenance') is not True
                        or not(group or direct) or not(owner or group and self.ding.get('groupMembersCanRequest') is True)):
                    raise BugFixerError('source_not_verified','Only verified original-group requests or the configured owner private request may start this delivery.')
            source_verified = True
        self.callers = sorted({(m['senderStaffId'], m['conversationType'], m['conversationId'])
                               for m in source.get('verifiedMessages', [])})
        self.callers = [{'staff_id': staff, 'channel': 'group' if kind == '2' else 'owner_dm',
                         'conversation_id': conversation} for staff, kind, conversation in self.callers]
        credential = writeback._credential(manifest['credential_ref'], self.database)
        if credential.account != self.owner['zenTaoAccount']:
            raise BugFixerError('credential_scope_mismatch', 'Delivery must use the configured repair identity.')
        if credential.password and credential.password in json.dumps(manifest, ensure_ascii=False):
            raise BugFixerError('secret_in_report', 'Remove the login secret from report fields.')
        wanted = scope(manifest)
        git = self.git_factory(wanted['project'], wanted['branch'])
        for f in wanted['files']:
            git.path(f)
        refs = list(dict.fromkeys(manifest['approval_ids'] + ['delivery-' + ident]))
        grants = []
        for ref in refs:
            row = self.host.records('approvals', 'maintenance', ref)
            if row and row['payload'].get('status') == 'approved' and row['payload'].get('approvedBy') == self.ding['ownerStaffId']:
                if any(row['payload'].get(k) in ('analysis','analysis_only') for k in ('task_mode','mode','requestMode')):
                    continue
                actions = row['payload'].get('deliveryScope',{}).get('actions')
                if actions is not None and not {'commit','push','writeback','notify_group'} <= set(actions):
                    continue
                if actions is not None and not {'commit','push','writeback','notify_group'} <= set(row['payload'].get('confirmed_actions', [])):
                    continue
                source_ref = row['payload'].get('sourceDispatchId')
                if source_ref and source_ref != manifest.get('source_dispatch_id'):
                    source_row = self.host.records('documents','dingTalkDispatch',source_ref)
                    original = source_row['payload'] if source_row else {}
                    if original.get('requestMode')=='analysis_only':
                        raise BugFixerError('analysis_only','An approval reference belongs to an analysis-only source.')
                    if original.get('status')=='completed' or original.get('maintenanceEnabled') is not True:
                        continue
                grants.append((ref, row['revision'], row['payload']))
        members = [] if authority_only else self.robot.members()
        self.authorization_builds = {}
        issues, selected = [], []
        for item in manifest['items']:
            bug = item['id']
            eligible = []
            for ref, _, p in grants:
                s = p.get('deliveryScope', p)
                ids = s.get('bugIds', [s.get('bugId')])
                if (bug in ids and str(Path(s.get('project', '')).resolve()) == wanted['project']
                        and s.get('branch') == wanted['branch'] and set(item['files']) <= set(s.get('files', []))):
                    with self.db() as c:
                        used = c.execute('SELECT job_id FROM delivery_approval_uses WHERE approval_id=? AND bug_id=?',(ref,bug)).fetchone()
                    if not used or used[0]==ident:
                        eligible.append({**p,'_ref':ref})
            # A normal domestic repair request already authorizes ordinary delivery.
            # Host-recorded source/scope evidence replaces a redundant owner approval.
            task = self.host.records('tasks','zentao',bug)
            ordinary = task['payload'].get('ordinaryDeliveryAuthorization',{}) if task else {}
            messages = [m for m in source.get('verifiedMessages',[]) if m.get('messageId')==ordinary.get('messageId')
                        and m.get('senderStaffId')==ordinary.get('requesterStaffId')]
            if (source_verified and ordinary.get('channel')=='domestic' and ordinary.get('mode')=='repair'
                    and ordinary.get('sourceDispatchId')==manifest.get('source_dispatch_id') and len(messages)==1
                    and str(ordinary.get('bugId'))==bug and ordinary.get('branch')==wanted['branch']
                    and str(Path(ordinary.get('project','')).resolve())==wanted['project']
                    and set(item['files'])<=set(ordinary.get('files',[]))
                    and not re.search('oversea',wanted['project']+' '+wanted['branch'],re.I)):
                ref='ordinary:'+manifest['source_dispatch_id']+':'+bug
                with self.db() as c:
                    used=c.execute('SELECT job_id FROM delivery_approval_uses WHERE approval_id=? AND bug_id=?',(ref,bug)).fetchone()
                if not used or used[0]==ident:
                    owner_request=messages[0]['senderStaffId']==self.ding['ownerStaffId']
                    metadata={}; metadata_refs=[]
                    for metadata_ref in list(ordinary.get('ownerMetadataApprovalIds',[]))+['delivery-'+ident]:
                        record=self.host.records('approvals','maintenance',metadata_ref)
                        p=record['payload'] if record else {}; s=p.get('deliveryScope',p)
                        if (p.get('status')=='approved' and p.get('approvedBy')==self.ding['ownerStaffId']
                                and p.get('deliveryJobId')==ident and bug in s.get('bugIds',[s.get('bugId')])
                                and s.get('branch')==wanted['branch'] and str(Path(s.get('project','')).resolve())==wanted['project']):
                            values=p.get('confirmed_metadata',{})
                            with self.db() as c:
                                consumed=c.execute('SELECT job_id FROM delivery_approval_uses WHERE approval_id=? AND bug_id=?',(metadata_ref,bug)).fetchone()
                            if isinstance(values,dict) and (not consumed or consumed[0]==ident):
                                metadata.update({k:v for k,v in values.items() if k in ('runtimeSelfTestWaived','notification_mode','resolvedBuild')})
                                metadata_refs.append(metadata_ref)
                    eligible.append({'_ref':ref,'resolvedBuild':ordinary.get('resolvedBuild'),
                                     'runtimeSelfTestWaived':metadata.get('runtimeSelfTestWaived') is True or owner_request and ordinary.get('runtimeSelfTestWaived') is True,
                                     'notification_mode':metadata.get('notification_mode') or (ordinary.get('notification_mode') if owner_request else None),
                                     **({'resolvedBuild':metadata['resolvedBuild']} if metadata.get('resolvedBuild') else {}),
                                     '_metadata_refs':metadata_refs})
            if not eligible:
                known_domestic=bool(re.search('intranational|domestic',wanted['project']+' '+wanted['branch'],re.I))
                issues.append(self.issue('existing_authorization_proof_required' if known_domestic else 'scoped_approval_required', bug))
            if manifest['validation']['mode'] == 'waived' and not any(p.get('runtimeSelfTestWaived') is True for p in eligible):
                issues.append(self.issue('scoped_test_waiver_required', bug))
            if manifest['notification_mode'] == 'group_only' and not any(p.get('notification_mode') == 'group_only' for p in eligible):
                issues.append(self.issue('group_only_notification_confirmation_required', bug))
            # Bind metadata choices to actual owner-approved fields, not the manifest alone.
            builds = {str(p.get('resolvedBuild') or p.get('resolved_builds', {}).get(bug, '')) for p in eligible} - {''}
            build = item['resolved_build'] or (next(iter(builds)) if len(builds) == 1 else '')
            self.authorization_builds[bug] = build
            if not build or build not in builds:
                issues.append(self.issue('resolved_build_confirmation_required', bug))
            if bug in completed:
                continue
            context = writeback._fresh(self.client, bug, credential)
            writeback._active(context, self.owner['zenTaoAccount'])
            forms = writeback._forms(self.client, bug, context)
            for issue in issues:
                if issue.get('id')==bug and issue['code']=='resolved_build_confirmation_required':
                    issue.update(choices=forms['builds'], proposed=build)
            if build not in forms['builds'] or not build:
                issues.append(self.issue('resolved_build_choice_required', bug, choices=forms['builds']))
            creator, name = context['creator'], context.get('users', {}).get(context['creator'])
            if not creator or creator not in forms['resolve'].options.get('assignedTo', {}):
                issues.append(self.issue('test_assignee_unavailable', bug))
            matches = [m for m in members if m.get('name') == name or m.get('memberEmpName') == name]
            if manifest['notification_mode'] != 'group_only' and (len(matches) != 1 or not matches[0].get('openDingtalkId')):
                issues.append(self.issue('creator_not_in_original_group', bug, creator=name or creator,
                                         supported_choice='explicitly approve group_only or use the original manual notification flow'))
            selected.append({'id': bug, 'creator': creator, 'creator_name': name, 'resolved_build': build,
                             'mention': matches[0].get('openDingtalkId') if len(matches) == 1 else None,
                             'bug_snapshot': writeback.snapshot(context),
                             'authority_ids': sorted({ref for p in eligible for ref in [p['_ref']]+p.get('_metadata_refs',[])})})
        if manifest['validation']['mode'] == 'verified':
            evidence = manifest['validation'].get('evidence')
            if not isinstance(evidence, str) or not Path(evidence).is_file():
                issues.append(self.issue('validation_evidence_required'))
        self.source_verified = source_verified or bool(grants)
        return git, wanted, issues, selected

    def inspect(self, ident, manifest, notify=False):
        manifest = validate(manifest)
        old = self.read(ident)
        wanted_hash = writeback.digest(scope(manifest))
        if old and old['scope_hash'] != wanted_hash:
            raise BugFixerError('job_conflict', 'A delivery ID cannot be reused for different Bugs, repository, branch or files.')
        if old:
            old_data=json.loads(old['data'])
            prewrite_only=set(old_data)<= {'error'}
            if old['busy'] or old_data and not prewrite_only:
                return self.status(ident)
        with self.db() as c:
            for other_id, other_plan in c.execute("SELECT id,plan FROM delivery_jobs WHERE id<>? AND state IN ('running','uncertain')", (ident,)):
                if set(json.loads(other_plan)['scope']['bugIds']).intersection(scope(manifest)['bugIds']):
                    raise BugFixerError('other_delivery_uncertain', 'A prior delivery for this Bug must be reconciled; changing job IDs is not a retry.')
        git, wanted, issues, selected = self.preflight(ident, manifest)
        git_error = None
        try:
            baseline = git.inspect(wanted['files'])
        except BugFixerError as error:
            baseline = None; git_error = {'code':error.code,'message':error.message}
            issues.append(self.issue(error.code,detail=error.message))
        if baseline and baseline['head'] != baseline['remote']:
            issues.append(self.issue('branch_not_synchronized', detail='Agent must reconcile origin safely; no automatic pull/rebase.'))
        if baseline and baseline['changed']:
            for item in manifest['items']:
                if not set(item['files']).intersection(baseline['changed']):
                    issues.append(self.issue('bug_has_no_selected_changes', item['id']))
        binding = {k: self.ding[k] for k in ('profile', 'robotCode', 'allowedGroupId', 'ownerStaffId')}
        if old and json.loads(old['plan'])['notification_binding'] != binding:
            raise BugFixerError('notification_binding_changed', 'Sender organization/group changes require explicit review, not automatic reinspection.')
        plan = {'manifest': manifest, 'git': baseline, 'selected': selected, 'scope': wanted, 'notification_binding': binding,
                'callers': self.callers,
                'commit_message': '修复Bug：' + '、'.join(wanted['bugIds']) + ' | BugFixer-Job: ' + ident}
        plan['hash'] = writeback.digest(plan)
        operational = git_error or any(i['code'] in ('branch_not_synchronized', 'bug_has_no_selected_changes', 'validation_evidence_required', 'test_assignee_unavailable','existing_authorization_proof_required') for i in issues)
        state = ('needs_agent' if operational else 'needs_approval') if issues else ('ready' if baseline['changed'] else 'ready_for_repair')
        plan['issues'] = issues
        with self.db() as c:
            c.execute('''INSERT INTO delivery_jobs VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                plan=excluded.plan,state=excluded.state,updated_at=excluded.updated_at WHERE delivery_jobs.busy=0''',
                      (ident, wanted_hash, json.dumps(plan, ensure_ascii=False), state, '{}', 0, now()))
        path = write_json(self.output/('delivery-' + ident)/('inspection-' + uuid.uuid4().hex[:10] + '.json'), plan)
        approval_id = 'delivery-' + ident
        if issues:
            record = self.host.records('approvals', 'maintenance', approval_id)
            pending = {'requestId': approval_id, 'deliveryJobId':ident, 'status': 'pending',
                       'deliveryScope': {**wanted,'actions':['commit','push','writeback','notify_group']},
                       'confirmed_actions': [], 'confirmed_metadata': {},
                       'issues': issues, 'planPath': path, 'createdAt': now(),
                       'requiredOwnerStaffId': self.ding['ownerStaffId'],
                       'note': 'Host must verify the real owner reply and fill approvedBy, resolved_builds, notification_mode and any explicit test waiver; parameters are not approval.'}
            if manifest.get('source_dispatch_id'):
                pending['sourceDispatchId'] = manifest['source_dispatch_id']
            if not record:
                self.host.put('approvals', 'maintenance', approval_id, pending, expected_revision=0)
            elif record['payload'].get('status') == 'pending':
                self.host.put('approvals', 'maintenance', approval_id, pending, expected_revision=record['revision'])
            if notify and state == 'needs_approval':
                if not self.source_verified:
                    raise BugFixerError('source_not_verified','Collect host-verified repair-request evidence before notifying an approval request.')
                self._notify_approval(ident, wanted, issues, path, manifest)
        return {'status': state, 'implementation_version':__version__, 'job_id': ident, 'plan_hash': plan['hash'], 'plan_path': path,
                'approval_id': approval_id if issues else None, 'issues': issues[:8], 'issue_count': len(issues),
                'bug_ids': wanted['bugIds'], 'repo_or_bug_writes': False,
                **({'error':git_error} if git_error else {}),
                'approval_notification_requested': bool(notify and state == 'needs_approval')}

    def _notify_approval(self, ident, wanted, issues, evidence, manifest):
        grouped = {}
        for issue in issues:
            grouped.setdefault(issue['code'], []).append(issue.get('id', 'batch'))
        labels = {'scoped_approval_required':'修复范围确认', 'scoped_test_waiver_required':'跳过自测确认',
                  'resolved_build_confirmation_required':'解决版本确认', 'resolved_build_choice_required':'选择真实解决版本',
                  'group_only_notification_confirmation_required':'仅保留原群反馈确认',
                  'creator_not_in_original_group':'创建人不在原群，请确认通知方式'}
        body = ('请求 delivery-' + ident + '\nBug：' + '、'.join(wanted['bugIds']) + '\n分支：' + wanted['branch']
                + '\n待确认：' + '；'.join(labels.get(k,k) + '(' + '、'.join(v) + ')' for k, v in grouped.items()))
        if 'scoped_approval_required' in grouped:
            for item in manifest['items']:
                names='、'.join(Path(f).name for f in item['files'][:2])
                body += '\n'+item['id']+'（'+names+'）：拟'+item['change'].replace('\n',' ')[:65]
        options = next((i.get('choices') for i in issues if i.get('choices')), None)
        if options:
            body += '\n解决版本选项：' + '、'.join(str(k)+'='+str(v) for k,v in list(options.items())[:12] if k)
        body += '\n请明确确认本请求的缺项；仅回答版本或通知方式不代表批准修复范围。当前未提交或回填。'
        for target in ('group', 'owner'):
            key = 'delivery:' + ident + ':approval:' + target
            recipient = self.ding['allowedGroupId'] if target == 'group' else self.ding['ownerStaffId']
            if not self.host.reserve_notification(key, 'maintenance', ident, recipient, {'kind':'delivery_approval'}):
                continue
            try:
                result = self.robot.send(body, owner_dm=target=='owner', owner_at=target=='group')
                receipt = write_json(self.output/('delivery-' + ident)/('approval-' + target + '.json'), result)
                self.host.notification_result(key, 'sent', {'readbackEvidence':receipt,'planEvidence':evidence})
            except BugFixerError as error:
                self.host.notification_result(key, 'unknown', {'readbackEvidence':evidence,'code':error.code,'note':'No automatic resend.'})

    def status(self, ident):
        row = self.read(ident)
        if not row:
            raise BugFixerError('job_not_found', 'Inspect a delivery before running it.')
        data = json.loads(row['data'])
        return {'status': row['state'], 'implementation_version':__version__, 'job_id': ident, 'stage': data.get('intent') or data.get('stage'),
                'commit': data.get('commit'), 'completed_bug_ids': sorted(data.get('writebacks', {})),
                'notification': data.get('notification'), 'error': data.get('error'),
                'notification_coverage': data.get('notification_coverage', []),
                'receipt_is_historical': row['state'] == 'completed'}

    def run(self, ident, plan_hash):
        row = self.read(ident)
        if not row:
            raise BugFixerError('job_not_found', 'Inspect this delivery first.')
        plan = json.loads(row['plan']); data = json.loads(row['data'])
        if writeback.digest({k:v for k,v in plan.items() if k not in ('hash', 'issues')}) != plan.get('hash'):
            raise BugFixerError('plan_conflict', 'The stored delivery plan failed its content hash.')
        if plan_hash != plan['hash']:
            raise BugFixerError('plan_conflict', 'Run must bind the exact inspected plan hash.')
        if row['state'] == 'completed':
            return self.status(ident)
        if row['state'] not in ('ready', 'uncertain', 'running') and not (row['state']=='needs_approval' and data):
            return self.status(ident)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE delivery_jobs SET busy=1 WHERE id=?', (ident,))
        claimed = False; attempted = False; resource = 'repo:' + plan['scope']['project'].replace('\\', '/')
        claim_owner = 'BugFixer-delivery:' + ident
        try:
            skip = set(data.get('writebacks', {})) | set(data.get('prepared', {}))
            git, _, issues, selected = self.preflight(ident, plan['manifest'], completed=skip)
            binding = {k: self.ding[k] for k in plan['notification_binding']}
            if binding != plan['notification_binding']:
                raise BugFixerError('notification_binding_changed', 'Original-group sender settings changed; no identity or organization switch was made.')
            if 'callers' in plan and self.callers != plan['callers']:
                raise BugFixerError('notification_caller_changed', 'Verified callers changed after inspection; no write was started.')
            if issues:
                data['error'] = {'code': 'authorization_or_metadata_changed', 'issues': issues[:8]}
                self.checkpoint(ident, 'needs_approval', data)
                return self.status(ident)
            for item in selected:
                if item != next(p for p in plan['selected'] if p['id'] == item['id']):
                    raise BugFixerError('bug_changed', 'A Bug or its notification identity changed after inspection.')
            for claim in self.host.db.execute('SELECT resource,owner FROM claims'):
                if plan['scope']['project'].replace('\\', '/').lower() in claim['resource'].replace('\\', '/').lower() and claim['owner'] != claim_owner:
                    raise BugFixerError('resource_busy', 'The project has another owner; no expired claim was taken over.')
            claimed = self.host.claim(resource, claim_owner, 1800)
            if not claimed:
                raise BugFixerError('resource_busy', 'The project could not be claimed.')
            message = plan.get('commit_message') or ('fix: ' + ', '.join('bug' + i for i in plan['scope']['bugIds']) + '\n\nBugFixer-Job: ' + ident)
            if not data.get('commit'):
                if data.get('intent') == 'commit':
                    sha = git.head()
                    if not git.matches_commit(sha, plan['git'], message):
                        raise BugFixerError('commit_uncertain', 'An earlier commit intent cannot be proved; no commit was repeated.')
                else:
                    self.guard(ident, plan)
                    if git.inspect(plan['scope']['files']) != plan['git']:
                        raise BugFixerError('worktree_conflict', 'Files or origin changed after inspection.')
                    with self.db() as c:
                        c.execute('BEGIN IMMEDIATE')
                        for item in plan['selected']:
                            for ref in item['authority_ids']:
                                used=c.execute('SELECT job_id FROM delivery_approval_uses WHERE approval_id=? AND bug_id=?',
                                               (ref,item['id'])).fetchone()
                                if used and used[0]!=ident:
                                    raise BugFixerError('approval_already_used','An approval already funded a different delivery; obtain a new scoped confirmation.')
                                c.execute('INSERT OR IGNORE INTO delivery_approval_uses VALUES(?,?,?)',(ref,item['id'],ident))
                    data['intent'] = 'commit'; self.checkpoint(ident, 'running', data)
                    attempted = True
                    sha = git.commit(plan['git'], message)
                data.update(commit=sha, actual_commit_message=git.call('log', '-1', '--format=%B', sha).strip(), stage='committed'); data.pop('intent', None)
                self.checkpoint(ident, 'running', data)
            if not data.get('remote_verified'):
                if data.get('intent') == 'push':
                    if not git.pushed(data['commit']):
                        raise BugFixerError('push_uncertain', 'The earlier push remains unproved; no push was repeated.')
                else:
                    self.guard(ident, plan)
                    data['intent'] = 'push'; self.checkpoint(ident, 'running', data)
                    attempted = True
                    git.push(data['commit'], plan['git']['remote'])
                data.update(remote_verified=True, stage='pushed'); data.pop('intent', None)
                self.checkpoint(ident, 'running', data)
            for item in plan['manifest']['items']:
                bug = item['id']
                if bug in data.get('writebacks', {}):
                    continue
                choice = next(x for x in plan['selected'] if x['id'] == bug)
                self.guard(ident, plan)
                request_id = 'delivery-' + ident + '-bug' + bug
                saved = data.setdefault('prepared', {}).get(bug)
                if not saved:
                    args = {'request_id': request_id, 'id': bug, 'credential_ref': plan['manifest']['credential_ref'],
                            'task_mode': 'repair', 'expected_assigned_to': self.owner['zenTaoAccount'],
                            'assigned_to': choice['creator'], 'resolved_build': choice['resolved_build'],
                            'branch': plan['scope']['branch'], 'commit': data['commit'],
                            **{k: item[k] for k in ('reason', 'change', 'acceptance')}}
                    prepared = writeback.prepare_writeback(args, database=self.database, client=self.client)
                    if prepared.get('status') != 'prepared':
                        raise BugFixerError('writeback_prepare_blocked', 'A Bug could not be prepared; inspect its existing request, do not change IDs.')
                    saved = {'request_id': request_id, 'preview_hash': prepared['preview_hash']}
                    data['prepared'][bug] = saved
                    self.checkpoint(ident, 'running', data)
                data['intent'] = 'writeback:' + bug; self.checkpoint(ident, 'running', data)
                result = writeback.apply_writeback(saved, database=self.database, output_root=self.output, client=self.client)
                attempted |= result.get('business_writes_this_call', result.get('business_write_attempted', False))
                if result.get('status') != 'completed' or result.get('verified') is not True:
                    data['writeback_issue'] = {'id': bug, 'result': result}
                    raise BugFixerError('writeback_uncertain', 'Writeback is not verified; subsequent Bugs were paused without replay.')
                for attempt in range(3):
                    task = self.host.records('tasks', 'zentao', bug)
                    payload = dict(task['payload']) if task else {}
                    payload.update(status='resolved_pending_test', commit=data['commit'], branch=plan['scope']['branch'],
                                   remoteVerified=True, writebackReadback=result.get('readback_path'), deliveryJobId=ident)
                    try:
                        if not task or payload != task['payload']:
                            self.host.put('tasks', 'zentao', bug, payload, expected_revision=task['revision'] if task else 0)
                        break
                    except ValueError as error:
                        if 'revision' not in str(error).lower() or attempt==2:
                            raise
                data.setdefault('writebacks', {})[bug] = result
                data.pop('intent', None); data['stage'] = 'writebacks'
                self.checkpoint(ident, 'running', data)
            if not data.get('notification'):
                attempted = True
                self.notify_result(ident, plan, data)
            data['intent']='notification_records'; self.checkpoint(ident, 'running', data)
            for choice in plan['selected']:
                for attempt in range(3):
                    task=self.host.records('tasks','zentao',choice['id'])
                    payload={**task['payload'], 'notificationCoverage':data['notification_coverage'], 'notificationEvidence':data['notification']}
                    try:
                        if payload!=task['payload']:
                            self.host.put('tasks','zentao',choice['id'],payload,expected_revision=task['revision'])
                        break
                    except ValueError as error:
                        if 'revision' not in str(error).lower() or attempt==2: raise
            data.pop('intent',None)
            data.pop('error', None); data['stage'] = 'completed'; self.checkpoint(ident, 'completed', data)
            return {**self.status(ident), 'receipt_is_historical':False, 'business_write_attempted_this_call': attempted}
        except (BugFixerError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as e:
            data['error'] = {'code': e.code if isinstance(e, BugFixerError) else 'pipeline_error',
                             'message': e.message if isinstance(e, BugFixerError) else 'A delivery stage could not be verified; no blind retry.'}
            state = 'needs_approval' if data['error']['code']=='authorization_changed' else ('uncertain' if data.get('intent') else 'conflict')
            self.checkpoint(ident, state, data)
            return {**self.status(ident), 'business_write_attempted_this_call': attempted}
        finally:
            if claimed:
                self.host.release(resource, claim_owner)
            with self.db() as c:
                c.execute('UPDATE delivery_jobs SET busy=0 WHERE id=?', (ident,))

    def notify_result(self, ident, plan, data):
        self.guard(ident, plan)
        current = {m.get('openDingtalkId') for m in self.robot.members()}
        if plan['manifest']['notification_mode'] != 'group_only' and any(x['mention'] not in current for x in plan['selected']):
            raise BugFixerError('notification_membership_changed', 'Creator membership changed; review before sending.')
        for choice in plan['selected']:
            # Prove the actual final assignee/creator before publishing delivery.
            fresh = writeback._fresh(self.client, choice['id'], writeback._credential(plan['manifest']['credential_ref'], self.database))
            if (fresh['status'] != 'resolved' or fresh['assigned_to'] != choice['creator']
                    or fresh['creator'] != choice['creator'] or fresh['bug'].get('resolution') != 'fixed'):
                raise BugFixerError('notification_assignee_changed', 'The final resolved assignee or creator changed; nothing was announced.')
        coverage = []
        for caller in plan.get('callers', []):
            coverage.append({'role':'caller', **caller})
        if not plan.get('callers'):
            coverage.append({'role':'caller','channel':'tool_return'})
        for choice in plan['selected']:
            coverage.append({'role':'creator', 'id':choice['id'], 'account':choice['creator'],
                             'channel':'group', 'open_id':choice['mention'],
                             'waived':plan['manifest']['notification_mode']=='group_only'})
        body = ('Bug ' + '、'.join(plan['scope']['bugIds']) + ' 已提交推送并解决转创建人验收。\n'
                + '原因：' + '；'.join(i['reason'] for i in plan['manifest']['items'])
                + '\n修改：' + '；'.join(i['change'] for i in plan['manifest']['items'])
                + '\n验收：已核对远端及工单保存；' + '；'.join(i['acceptance'] for i in plan['manifest']['items'])
                + '\n提交：' + data['commit'][:12] + '\n'
                + '\n'.join(writeback.BASE + '/bug-view-' + i + '.html' for i in plan['scope']['bugIds']))
        deliveries = [('group', False)]
        if any(c.get('channel')=='owner_dm' for c in coverage):
            deliveries.append(('owner', True))
        for target, owner_dm in deliveries:
            self.guard(ident, plan)
            recipients = [c for c in coverage if c.get('channel') == ('owner_dm' if owner_dm else 'group') and not c.get('waived')]
            users = sorted({c['staff_id'] for c in recipients if c.get('staff_id')})
            mentions = {c['open_id'] for c in recipients if c.get('open_id')}
            # Both roles can name the configured owner; address that identity once.
            if self.ding['ownerStaffId'] in users:
                mentions -= {c['open_id'] for c in recipients if c.get('account') == self.owner['zenTaoAccount']}
            key = 'delivery:' + ident + ':result' + (':owner' if owner_dm else '')
            saved = data.setdefault('notifications', {}).setdefault(target, {})
            if saved.get('verified'):
                continue
            since = saved.setdefault('since', now())
            if not saved.get('attempted'):
                if not self.host.reserve_notification(key, 'maintenance', ident,
                        self.ding['ownerStaffId'] if owner_dm else self.ding['allowedGroupId'],
                        {'kind':'repair_delivery', 'coverage':recipients}):
                    # Includes older jobs: reconcile without replaying an existing intent.
                    saved['attempted'] = True
                else:
                    saved['attempted'] = True
                    data['intent']='notification:' + target; self.checkpoint(ident, 'running', data)
                    try:
                        receipt = self.robot.send(body, sorted(mentions), user_ids=users, owner_dm=owner_dm)
                    except BugFixerError as error:
                        self.host.notification_result(key, 'failed' if error.code=='dws_before_send' else 'unknown',
                            {'code':error.code, 'coverage':recipients, 'classification':'failed_before_send' if error.code=='dws_before_send' else 'unknown'})
                        raise
                    saved['receipt'] = receipt
                    self.checkpoint(ident, 'running', data)
            data['intent']='notification_readback:' + target; self.checkpoint(ident, 'running', data)
            result = self.robot.verify(body, since, **({'owner_dm':True, 'conversation_id':recipients[0]['conversation_id']} if owner_dm else {}))
            saved.update(verified=True, readback=result)
            for recipient in recipients:
                recipient.update(status='sent', full_body_verified=True, message_id=result['message_id'])
            # On resume, rebuild coverage without losing prior per-role proof.
            saved['coverage'] = recipients
            path = write_json(self.output/('delivery-' + ident)/('notification-' + target + '.json'),
                              {'body':body, **saved})
            self.host.notification_result(key, 'sent', {'readbackEvidence':path,'coverage':recipients})
            data.pop('intent', None); self.checkpoint(ident, 'running', data)
        coverage = [c for n in data['notifications'].values() for c in n.get('coverage', [])] + [
            {**c, 'status':'waived' if c.get('waived') else 'returned'} for c in coverage if c.get('waived') or c.get('channel')=='tool_return']
        data['notification_coverage'] = coverage
        data['notification'] = write_json(self.output/('delivery-' + ident)/'notification.json', {'coverage':coverage})
        self.checkpoint(ident, 'running', data)

    def guard(self, ident, plan):
        _,_,issues,_ = self.preflight(ident, plan['manifest'], completed=plan['scope']['bugIds'], authority_only=True)
        if issues or self.authorization_builds != {i['id']:i['resolved_build'] for i in plan['selected']}:
            raise BugFixerError('authorization_changed','A scoped approval or metadata choice changed; no next write was started.')
        if {k:self.ding[k] for k in plan['notification_binding']} != plan['notification_binding']:
            raise BugFixerError('notification_binding_changed','Sender organization/group changes require explicit review.')
        if 'callers' in plan and self.callers != plan['callers']:
            raise BugFixerError('notification_caller_changed','Verified callers changed; no next write was started.')


def deliver_repair(request, **options):
    pipeline = None
    try:
        if (not isinstance(request, dict) or set(request) - {'mode', 'job_id', 'manifest', 'plan_hash', 'notify_approval'}
                or request.get('mode') not in ('inspect', 'run', 'status')
                or not isinstance(request.get('job_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request['job_id'])):
            raise BugFixerError('invalid_request', 'Use inspect/run/status and a simple delivery job ID.')
        keys = {'inspect':{'mode','job_id','manifest','notify_approval'}, 'run':{'mode','job_id','plan_hash'}, 'status':{'mode','job_id'}}
        if set(request) - keys[request['mode']]:
            raise BugFixerError('invalid_request', 'These fields do not belong to this delivery operation.')
        pipeline = Pipeline(**options)
        if request['mode'] == 'inspect':
            if not isinstance(request.get('notify_approval', False), bool):
                raise BugFixerError('invalid_request', 'notify_approval must be a boolean.')
            return pipeline.inspect(request['job_id'], request.get('manifest'), request.get('notify_approval', False))
        if request['mode'] == 'run':
            with job_lock(pipeline.database, request['job_id']):
                return pipeline.run(request['job_id'], request.get('plan_hash'))
        return {**pipeline.status(request['job_id']), 'business_writes_this_call': False}
    except (BugFixerError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as e:
        return {'status': 'needs_agent', 'error': {'code': e.code if isinstance(e, BugFixerError) else 'delivery_error',
                'message': e.message if isinstance(e, BugFixerError) else 'Delivery could not be verified; no fallback mutation was performed.'}}
    finally:
        if pipeline:
            pipeline.close()


DELIVERY_SCHEMA = {'name': 'deliver_repair',
    'description': 'Inspect or run scoped repair delivery: Git hook-compatible commit/push, existing approval checks, verified ZenTao writeback to the Bug creator, and caller/creator notifications with read-only receipt recovery. Never approves, force-pushes, changes identity/org or fixes code. Only invoke for authorized maintenance.',
    'inputSchema': {'type': 'object', 'properties': {'mode': {'enum': ['inspect', 'run', 'status']},
        'job_id': {'type': 'string'}, 'manifest': {'type': 'object'}, 'plan_hash': {'type': 'string'},
        'notify_approval': {'type': 'boolean', 'default': False}}, 'required': ['mode', 'job_id'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': True}}
