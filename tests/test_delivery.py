import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from bugfixer.common import BugFixerError, Credential
from bugfixer.delivery import deliver_repair, Pipeline, job_lock
from bugfixer.delivery_git import Git
from bugfixer.delivery_host import workspace, Robot
from tests.test_writeback import FakeWriteZen


class RobotFixture:
    def __init__(self):
        self.sent = []; self.fail = False; self.people = [{'name': '测试', 'openDingtalkId': 'fixture-open-id'}]
    def members(self):
        return self.people
    def send(self, body, mentions=(), *, user_ids=(), owner_dm=False, owner_at=False):
        self.sent.append((body, list(mentions)))
        self.last_users = list(user_ids)
        self.__dict__.setdefault('targets', []).append({'users':list(user_ids),'owner_dm':owner_dm})
        if self.fail:
            raise BugFixerError('dws_uncertain', 'Fixture response lost after acceptance.')
        return {'ok': True, 'failedCount': 0, 'succeededCount': 1}
    def verify(self, body, since, **options):
        if self.fail or not self.sent or getattr(self, 'readback_pending', False):
            raise BugFixerError('notification_readback_pending', 'Fixture readback is not yet available.')
        return {'full_body_verified':True, 'message_id':'fixture-message-sent', 'conversation_id':'fixture-group'}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1]/'acceptance'/'pipeline-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        assert Path(self.temp.name).resolve().is_relative_to(base.resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = self.root/'repo'; self.repo.mkdir()
        self.remote = self.root/'origin.git'
        self.git('init', '--bare', str(self.remote), cwd=self.root)
        self.git('init'); self.git('symbolic-ref', 'HEAD', 'refs/heads/dev')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        hooks = self.root/'empty-hooks'; hooks.mkdir(); self.git('config', 'core.hooksPath', str(hooks))
        self.file = 'Assets/LoadResources/UI/Test.prefab'
        target = self.repo/self.file; target.parent.mkdir(parents=True); target.write_text('old\n')
        (self.repo/'unrelated.txt').write_text('initial\n')
        self.git('add', '.'); self.git('commit', '-m', 'fixture baseline')
        self.git('remote', 'add', 'origin', str(self.remote)); self.git('push', '-u', 'origin', 'dev')
        target.write_text('new\n'); (self.repo/'unrelated.txt').write_text('other user work\n')
        self.host = workspace(self.root/'workspace.sqlite3'); self.addCleanup(self.host.close)
        for section, value in {'runtime': {'maintenanceEnabled': True}, 'owner': {'zenTaoAccount': 'repairer'},
            'dingTalk': {'ownerStaffId': 'owner', 'profile': 'fixture-profile', 'allowedGroupId': 'fixture-group', 'robotCode': 'fixture-bot'}}.items():
            self.host.config_set(section, value, expected_revision=0)
        self.host.config_set('desktopDispatch',{'zentaoThreadId':'fixture-thread'},expected_revision=0)
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',{'requestMode':'maintenance','route':'zentao_maintainer',
          'maintenanceEnabled':True,'targetThreadId':'fixture-thread','desktopReceiverThreadId':'fixture-thread','status':'running',
          'verifiedMessages':[{'senderStaffId':'owner','conversationId':'fixture-group','conversationType':'2',
                               'messageId':'fixture-message','canRequestMaintenance':True,'text':'修复指定Bug'}]},expected_revision=0)
        self.client = FakeWriteZen(); self.robot = RobotFixture()
        self.git_factory = Git
        self.auth = patch('bugfixer.writeback._credential', return_value=Credential('repairer', 'fixture-secret-password'))
        self.auth.start(); self.addCleanup(self.auth.stop)
        self.manifest = {'task_mode': 'repair', 'repo': str(self.repo), 'branch': 'dev', 'credential_ref': 'fixture',
            'source_dispatch_id':'fixture-dispatch',
            'approval_ids': ['approved-bug12'], 'notification_mode': 'creator_in_group', 'validation': {'mode': 'waived'},
            'items': [{'id': '12', 'files': [self.file], 'reason': '文本范围不足', 'change': '调整文本范围',
                       'acceptance': '请复验长文案', 'resolved_build': 'trunk'}]}
        self.grant = {'status': 'approved', 'approvedBy': 'owner', 'project': str(self.repo), 'branch': 'dev',
            'bugId': '12', 'files': [self.file], 'runtimeSelfTestWaived': True, 'resolvedBuild': 'trunk'}
        self.host.put('approvals', 'maintenance', 'approved-bug12', self.grant, expected_revision=0)

    def git(self, *args, cwd=None):
        p = subprocess.run(['git', '-C', str(cwd or self.repo), *args], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def call(self, mode, **args):
        return deliver_repair({'mode': mode, 'job_id': 'fixture-delivery', **args}, database=self.root/'tool.sqlite3',
            output_root=self.root/'artifacts', host=self.host, robot=self.robot, client=self.client, git_factory=self.git_factory)

    def inspect(self, **extra):
        return self.call('inspect', manifest=self.manifest, **extra)

    def run_job(self, plan):
        self.assertEqual(plan['status'], 'ready', plan)
        return self.call('run', plan_hash=plan['plan_hash'])

    def update_grant(self, **changes):
        row = self.host.records('approvals', 'maintenance', 'approved-bug12')
        self.host.put('approvals', 'maintenance', 'approved-bug12', {**row['payload'], **changes}, expected_revision=row['revision'])

    def test_real_git_end_to_end_one_run_and_duplicate_never_replays(self):
        before = self.git('rev-parse', 'HEAD'); plan = self.inspect()
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        result = self.run_job(plan)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['implementation_version'],'0.3.1')
        self.assertTrue(result['business_write_attempted_this_call'])
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/dev').split()[0], result['commit'])
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD'), self.file)
        self.assertEqual((self.repo/'unrelated.txt').read_text(), 'other user work\n')
        self.assertEqual(self.git('diff', '--name-only'), 'unrelated.txt')
        self.assertEqual(self.client.bug['status'], 'resolved'); self.assertEqual(self.client.bug['assignedTo'], 'tester')
        self.assertEqual(len(self.robot.sent), 1); self.assertEqual(len(self.client.posts), 2)
        duplicate = self.call('run', plan_hash=plan['plan_hash'])
        self.assertEqual(duplicate['status'], 'completed')
        self.assertTrue(duplicate['receipt_is_historical'])
        self.assertEqual(len(self.client.posts), 2); self.assertEqual(len(self.robot.sent), 1)
        self.assertEqual(self.host.records('tasks', 'zentao', '12')['payload']['status'], 'resolved_pending_test')
        self.assertEqual(list(self.host.db.execute('SELECT * FROM claims')), [])

    def test_all_approval_gaps_collected_and_only_one_request_notification(self):
        self.update_grant(status='pending', resolvedBuild='', runtimeSelfTestWaived=False)
        self.robot.people = []
        first = self.inspect(notify_approval=True)
        self.assertEqual(first['status'], 'needs_approval', first)
        codes = {x['code'] for x in first['issues']}
        self.assertIn('scoped_approval_required', codes)
        self.assertIn('scoped_test_waiver_required', codes)
        self.assertIn('resolved_build_confirmation_required', codes)
        self.assertIn('creator_not_in_original_group', codes)
        self.inspect(notify_approval=True)
        self.assertEqual(len(self.robot.sent), 2)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.host.records('approvals','maintenance','delivery-fixture-delivery')['payload']['status'], 'pending')

    def test_supplied_parameters_and_wrong_owner_do_not_grant_approval(self):
        self.update_grant(approvedBy='other')
        plan = self.inspect()
        self.assertEqual(plan['status'], 'needs_approval')
        self.assertEqual(self.call('run', plan_hash=plan['plan_hash'])['status'], 'needs_approval')
        self.assertEqual(self.client.posts, [])
        self.assertFalse(self.robot.sent)

    def test_revocation_before_run_stops_without_committing(self):
        plan = self.inspect(); before = self.git('rev-parse', 'HEAD')
        self.update_grant(status='rejected')
        self.assertEqual(self.run_job(plan)['status'], 'needs_approval')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.update_grant(status='approved')
        self.assertEqual(self.call('run',plan_hash=plan['plan_hash'])['status'],'completed')

    def test_changed_files_or_plan_hash_cannot_be_delivered(self):
        plan = self.inspect(); (self.repo/self.file).write_text('changed after inspection\n')
        result = self.run_job(plan)
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['error']['code'], 'worktree_conflict')
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.run_job(self.inspect())['status'],'completed')

    def test_existing_staging_is_not_reset_or_submitted(self):
        self.git('add', 'unrelated.txt')
        result = self.inspect()
        self.assertEqual(result['error']['code'], 'staged_changes')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'unrelated.txt')

    def test_special_files_and_analysis_mode_are_rejected(self):
        self.manifest['task_mode'] = 'analysis'
        self.assertEqual(self.inspect()['error']['code'], 'invalid_manifest')
        self.manifest['task_mode'] = 'repair'; self.manifest['items'][0]['files'] = ['Assets/Scripts/Main/AOT.cs']
        self.update_grant(files=self.manifest['items'][0]['files'])
        self.assertEqual(self.inspect()['error']['code'], 'special_scope_unsupported')
        self.assertEqual(self.client.posts, [])

    def test_other_or_expired_owner_claim_is_not_taken_over(self):
        plan = self.inspect()
        self.host.claim('unity:' + self.repo.as_posix(), 'other-task', 1)
        result = self.run_job(plan)
        self.assertEqual(result['error']['code'], 'resource_busy')
        self.assertEqual(self.client.posts, [])

    def test_timeout_after_resolve_resumes_by_reading_not_replaying(self):
        self.client.fail_after = 'resolve'; plan = self.inspect()
        first = self.run_job(plan)
        self.assertEqual(first['status'], 'uncertain', first)
        self.assertEqual(len(self.client.posts), 2)
        second = self.call('run', plan_hash=plan['plan_hash'])
        self.assertEqual(second['status'], 'completed', second)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(len(self.robot.sent), 1)

    def test_lost_notification_receipt_never_resends(self):
        self.robot.fail = True; plan = self.inspect()
        self.assertEqual(self.run_job(plan)['status'], 'uncertain')
        second = self.call('run', plan_hash=plan['plan_hash'])
        self.assertEqual(second['status'], 'uncertain')
        self.assertEqual(len(self.robot.sent), 1)

    def test_group_only_requires_explicit_confirmation_not_a_manifest_flag(self):
        self.manifest['notification_mode'] = 'group_only'; self.robot.people = []
        plan = self.inspect()
        self.assertIn('group_only_notification_confirmation_required', {i['code'] for i in plan['issues']})
        self.update_grant(notification_mode='group_only')
        plan = self.inspect()
        self.assertEqual(self.run_job(plan)['status'], 'completed')
        self.assertEqual(self.robot.sent[0][1], [])
        self.assertTrue(any(c['role']=='creator' and c['status']=='waived' for c in self.call('status')['notification_coverage']))

    def test_notification_settings_change_cannot_switch_organization(self):
        plan = self.inspect()
        row = self.host.config('dingTalk');p=row['payload'];p['profile']='different-company'
        self.host.config_set('dingTalk',p,expected_revision=row['revision'])
        result = self.run_job(plan)
        self.assertEqual(result['error']['code'], 'notification_binding_changed')
        self.assertEqual(self.client.posts, []);self.assertFalse(self.robot.sent)

    def test_pending_scoped_approval_can_resume_without_recreating_the_job(self):
        self.update_grant(status='pending')
        self.assertEqual(self.inspect()['status'], 'needs_approval')
        self.update_grant(status='approved')
        ready = self.inspect()
        self.assertEqual(self.run_job(ready)['status'], 'completed')

    def test_lock_prevents_concurrent_runner(self):
        plan = self.inspect()
        with job_lock(self.root/'tool.sqlite3', 'fixture-delivery'):
            result = self.call('run', plan_hash=plan['plan_hash'])
            self.assertEqual(result['error']['code'], 'job_busy')
        self.assertEqual(self.client.posts, [])

    def test_two_bugs_share_one_exact_commit_and_one_group_message(self):
        clients = {'12': self.client, '13': FakeWriteZen()}
        clients['13'].bug['id'] = '13'
        class MultiClient:
            def get(self, url):
                return clients[re.search(r'/bug-\w+-(\d+)',url).group(1)].get(url)
            def post_write_form(self, url, values):
                return clients[re.search(r'/bug-\w+-(\d+)',url).group(1)].post_write_form(url, values)
        self.client = MultiClient()
        self.manifest['items'].append({**self.manifest['items'][0], 'id':'13'})
        self.update_grant(bugIds=['12','13'])
        plan = self.inspect(); result = self.run_job(plan)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['completed_bug_ids'], ['12','13'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(len(self.robot.sent), 1)
        self.assertEqual(self.robot.sent[0][1],['fixture-open-id'])
        self.assertTrue(all(c.bug['status']=='resolved' and len(c.posts)==2 for c in clients.values()))

    def test_commit_response_loss_is_proved_without_another_commit(self):
        counter = {'commit':0}
        class LostCommit(Git):
            def commit(g, baseline, message):
                counter['commit'] += 1
                super().commit(baseline, message)
                raise BugFixerError('git_timeout', 'Fixture lost response after commit.')
        self.git_factory = LostCommit
        plan = self.inspect()
        self.assertEqual(self.run_job(plan)['status'], 'uncertain')
        result = self.call('run', plan_hash=plan['plan_hash'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(counter['commit'], 1)

    def test_push_response_loss_is_proved_without_another_push(self):
        counter = {'push':0}
        class LostPush(Git):
            def push(g, sha, remote):
                counter['push'] += 1
                super().push(sha, remote)
                raise BugFixerError('git_timeout', 'Fixture lost response after push.')
        self.git_factory = LostPush
        plan = self.inspect()
        self.assertEqual(self.run_job(plan)['status'], 'uncertain')
        result = self.call('run', plan_hash=plan['plan_hash'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(counter['push'], 1)

    def test_changing_job_id_cannot_bypass_an_uncertain_write(self):
        self.client.fail_after='edit'; plan=self.inspect()
        self.assertEqual(self.run_job(plan)['status'], 'uncertain')
        result = deliver_repair({'mode':'inspect','job_id':'different-id','manifest':self.manifest},
             database=self.root/'tool.sqlite3',output_root=self.root/'artifacts',host=self.host,robot=self.robot,client=self.client)
        self.assertEqual(result['error']['code'], 'other_delivery_uncertain')
        self.assertEqual(len(self.client.posts), 1)

    def test_crlf_clean_filter_is_verified_using_git_blob_not_raw_bytes(self):
        self.git('config','core.autocrlf','true')
        (self.repo/self.file).write_bytes(b'converted\r\n')
        plan=self.inspect(); result=self.run_job(plan)
        self.assertEqual(result['status'], 'completed', result)

    def test_analysis_source_cannot_be_upgraded_by_a_repair_manifest(self):
        row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload'];p['requestMode']='analysis_only'
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        result=self.inspect(notify_approval=True)
        self.assertEqual(result['error']['code'],'analysis_only')
        self.assertEqual(self.client.posts,[]);self.assertEqual(self.robot.sent,[])

    def test_a_completed_approval_is_not_a_permanent_grant_for_another_job(self):
        plan=self.inspect();self.assertEqual(self.run_job(plan)['status'],'completed')
        self.client=FakeWriteZen()
        (self.repo/self.file).write_text('a different later repair\n')
        later=deliver_repair({'mode':'inspect','job_id':'later-repair','manifest':self.manifest},
           database=self.root/'tool.sqlite3',output_root=self.root/'artifacts',host=self.host,robot=self.robot,client=self.client)
        self.assertEqual(later['status'],'needs_approval',later)
        self.assertIn('scoped_approval_required',{i['code'] for i in later['issues']})

    def test_metadata_only_reply_cannot_approve_the_requested_actions(self):
        self.update_grant(status='pending')
        self.inspect()
        record=self.host.records('approvals','maintenance','delivery-fixture-delivery');p=record['payload']
        p.update(status='approved',approvedBy='owner',runtimeSelfTestWaived=True,resolved_builds={'12':'trunk'})
        self.host.put('approvals','maintenance',record['record_id'],p,expected_revision=record['revision'])
        self.assertEqual(self.inspect()['status'],'needs_approval')
        record=self.host.records('approvals','maintenance','delivery-fixture-delivery');p=record['payload']
        p['confirmed_actions']=['commit','push','writeback','notify_group']
        self.host.put('approvals','maintenance',record['record_id'],p,expected_revision=record['revision'])
        self.assertEqual(self.run_job(self.inspect())['status'],'completed')

    def test_approval_revoked_after_commit_prevents_push_until_reapproved(self):
        host=self.host
        class RevokeAfterCommit(Git):
            def commit(g, baseline, message):
                sha=super().commit(baseline,message)
                row=host.records('approvals','maintenance','approved-bug12');p=row['payload'];p['status']='rejected'
                host.put('approvals','maintenance','approved-bug12',p,expected_revision=row['revision'])
                return sha
        self.git_factory=RevokeAfterCommit
        plan=self.inspect();old=self.git('ls-remote','origin','refs/heads/dev').split()[0]
        first=self.run_job(plan)
        self.assertEqual(first['status'],'needs_approval',first)
        self.assertEqual(self.git('ls-remote','origin','refs/heads/dev').split()[0],old)
        self.assertEqual(self.client.posts,[])
        self.update_grant(status='approved')
        self.assertEqual(self.call('run',plan_hash=plan['plan_hash'])['status'],'completed')

    def test_domestic_ordinary_request_uses_existing_authority_without_extra_owner_approval(self):
        self.update_grant(status='pending')
        self.manifest['approval_ids']=[]
        proof={'channel':'domestic','mode':'repair','sourceDispatchId':'fixture-dispatch','messageId':'fixture-message',
               'requesterStaffId':'owner','bugId':'12','project':str(self.repo),'branch':'dev','files':[self.file],
               'resolvedBuild':'trunk','runtimeSelfTestWaived':True}
        self.host.put('tasks','zentao','12',{'ordinaryDeliveryAuthorization':proof},expected_revision=0)
        ready=self.inspect()
        self.assertEqual(ready['status'],'ready',ready)
        self.assertIsNone(self.host.records('approvals','maintenance','delivery-fixture-delivery'))
        self.assertEqual(self.run_job(ready)['status'],'completed')

    def test_member_ordinary_repair_does_not_allow_forged_test_waiver(self):
        self.update_grant(status='pending');self.manifest['approval_ids']=[]
        row=self.host.config('dingTalk');p=row['payload'];p['groupMembersCanRequest']=True
        self.host.config_set('dingTalk',p,expected_revision=row['revision'])
        row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload']
        p['verifiedMessages'][0].update(senderStaffId='member',canApprove=True)
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        proof={'channel':'domestic','mode':'repair','sourceDispatchId':'fixture-dispatch','messageId':'fixture-message',
          'requesterStaffId':'member','bugId':'12','project':str(self.repo),'branch':'dev','files':[self.file],
          'resolvedBuild':'trunk','runtimeSelfTestWaived':True}
        self.host.put('tasks','zentao','12',{'ordinaryDeliveryAuthorization':proof},expected_revision=0)
        pending=self.inspect()
        self.assertIn('scoped_test_waiver_required',{i['code'] for i in pending['issues']})
        evidence=self.repo/'validation.txt';evidence.write_text('Host-verified fixture result')
        self.manifest['validation']={'mode':'verified','evidence':str(evidence)}
        self.assertEqual(self.run_job(self.inspect())['status'],'completed')

    def test_owner_waiver_metadata_combines_with_existing_ordinary_authority(self):
        self.update_grant(status='pending');self.manifest['approval_ids']=[]
        row=self.host.config('dingTalk');p=row['payload'];p['groupMembersCanRequest']=True
        self.host.config_set('dingTalk',p,expected_revision=row['revision'])
        row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload']
        p['verifiedMessages'][0].update(senderStaffId='member',canApprove=False)
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        proof={'channel':'domestic','mode':'repair','sourceDispatchId':'fixture-dispatch','messageId':'fixture-message',
          'requesterStaffId':'member','bugId':'12','project':str(self.repo),'branch':'dev','files':[self.file],'resolvedBuild':'trunk'}
        self.host.put('tasks','zentao','12',{'ordinaryDeliveryAuthorization':proof},expected_revision=0)
        self.assertEqual(self.inspect()['status'],'needs_approval')
        row=self.host.records('approvals','maintenance','delivery-fixture-delivery');p=row['payload']
        p.update(status='approved',approvedBy='owner',confirmed_metadata={'runtimeSelfTestWaived':True})
        self.host.put('approvals','maintenance',row['record_id'],p,expected_revision=row['revision'])
        self.assertEqual(p['confirmed_actions'],[])
        self.assertEqual(self.run_job(self.inspect())['status'],'completed')

    def install_hook(self, text):
        hook = Path(self.git('config','--get','core.hooksPath'))/'commit-msg'
        hook.write_text('#!/bin/sh\n' + text + '\n', encoding='utf-8', newline='\n')
        hook.chmod(0o755)

    def test_real_hook_flattened_multiline_message_keeps_scope_and_pushes(self):
        self.install_hook('msg=$(cat "$1"); echo $msg > "$1"')
        git=Git(str(self.repo),'dev');baseline=git.inspect([self.file])
        message='修复Bug：12\n\nBugFixer-Job: fixture-delivery'
        sha=git.commit(baseline,message)
        self.assertTrue(git.matches_commit(sha,baseline,message))
        git.push(sha,baseline['remote'])
        self.assertEqual(git.remote(),sha)
        self.assertEqual(self.git('log','-1','--format=%B'), '修复Bug：12 BugFixer-Job: fixture-delivery')

    def test_hook_semantic_rewrite_is_not_treated_as_whitespace(self):
        self.install_hook('echo "修复Bug：999 unauthorized" > "$1"')
        before=self.git('ls-remote','origin','refs/heads/dev').split()[0]
        plan=self.inspect(); result=self.run_job(plan)
        self.assertEqual(result['error']['code'],'commit_mismatch')
        self.assertEqual(self.git('ls-remote','origin','refs/heads/dev').split()[0],before)
        self.assertEqual(self.client.posts,[])
        self.assertEqual(self.call('run',plan_hash=plan['plan_hash'])['error']['code'],'commit_uncertain')
        self.assertEqual(self.git('rev-list','--count','HEAD'),'2')

    def test_one_run_notifies_verified_caller_and_creator_and_records_each_role(self):
        self.manifest['notification_mode']='caller_and_creator'
        result=self.run_job(self.inspect())
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.robot.targets,[{'users':['owner'],'owner_dm':False}])
        self.assertEqual(self.robot.sent[0][1],['fixture-open-id'])
        self.assertEqual({c['role'] for c in result['notification_coverage']},{'caller','creator'})
        self.assertTrue(all(c['status']=='sent' and c['full_body_verified'] for c in result['notification_coverage']))
        self.assertIn('修复Bug：12',self.git('log','-1','--format=%B'))
        task=self.host.records('tasks','zentao','12')['payload']
        self.assertEqual(task['notificationCoverage'],result['notification_coverage'])

    def test_readback_delay_resumes_readonly_without_another_send(self):
        self.robot.readback_pending=True;plan=self.inspect()
        first=self.run_job(plan)
        self.assertEqual(first['error']['code'],'notification_readback_pending')
        self.robot.readback_pending=False
        result=self.call('run',plan_hash=plan['plan_hash'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(self.robot.sent),1);self.assertEqual(len(self.client.posts),2)

    def test_lost_send_response_is_reconciled_by_readback_not_replayed(self):
        self.robot.fail=True;plan=self.inspect();self.run_job(plan)
        self.robot.fail=False
        result=self.call('run',plan_hash=plan['plan_hash'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(self.robot.sent),1)

    def test_caller_change_after_inspect_cannot_redirect_notification(self):
        plan=self.inspect();row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload']
        p['verifiedMessages'][0]['messageId']='new-request';p['verifiedMessages'][0]['conversationType']='1'
        p['verifiedMessages'][0]['conversationId']='owner-private'
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        result=self.run_job(plan)
        self.assertEqual(result['error']['code'],'notification_caller_changed')
        self.assertEqual(self.client.posts,[]);self.assertEqual(self.robot.sent,[])

    def test_verified_owner_private_caller_receives_only_original_owner_dm(self):
        row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload']
        p['verifiedMessages'][0].update(conversationType='1',conversationId='original-owner-private')
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        result=self.run_job(self.inspect())
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual([t['owner_dm'] for t in self.robot.targets],[False,True])
        self.assertEqual(len(result['notification_coverage']),2)

    def test_final_assignee_is_read_back_before_notifying(self):
        members=self.robot.members
        def changed():
            if self.client.bug['status']=='resolved':self.client.bug['assignedTo']='someone-else'
            return members()
        self.robot.members=changed
        result=self.run_job(self.inspect())
        self.assertEqual(result['error']['code'],'notification_assignee_changed')
        self.assertEqual(self.robot.sent,[])

    def test_real_git_hook_and_real_dws_subprocess_complete_without_agent_fallback(self):
        self.install_hook('msg=$(cat "$1"); echo $msg > "$1"')
        script=self.root/'dws_fixture.py'
        script.write_text('''import json,sys
from pathlib import Path
args=sys.argv[1:];root=Path(__file__).parent
if "+chat-members-list" in args:
 result={"complete":True,"users":[{"name":"测试","openDingtalkId":"fixture-open-id"}]}
elif "+messages-send" in args:
 raw=sys.stdin.buffer.read();assert b"\\r" not in raw
 body=raw.decode("utf-8");assert "文本范围不足" in body
 assert args[args.index("--at-user-ids")+1]=="owner"
 assert args[args.index("--at-open-dingtalk-ids")+1]=="fixture-open-id"
 if "--dry-run" in args:result={"dry_run":True,"executed":False,"body":body}
 else:
  path=root/"actually_sent.json";assert not path.exists(),"never resend"
  path.write_text(json.dumps({"body":body,"args":args}),encoding="utf-8")
  result={"ok":True,"succeededCount":1,"failedCount":0,"succeeded":[{"target":"fixture-group","result":{"success":True}}]}
elif args[:3]==["chat","message","list"]:
 body=json.loads((root/"actually_sent.json").read_text(encoding="utf-8"))["body"]
 result={"messages":[{"openConversationId":"fixture-group","sendType":"robot","content":" ".join(body.split()),"openMessageId":"full-flow-message"}]}
else:raise AssertionError(args)
print(json.dumps(result))
''',encoding='utf-8')
        self.robot=Robot(self.host.config('dingTalk')['payload'],command=[sys.executable,str(script)])
        plan=self.inspect();result=self.run_job(plan)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual({c['role'] for c in result['notification_coverage']},{'caller','creator'})
        self.assertTrue(all(c['message_id']=='full-flow-message' for c in result['notification_coverage']))
        again=self.call('run',plan_hash=plan['plan_hash'])
        self.assertEqual(again['status'],'completed')
        self.assertEqual(self.git('rev-list','--count','HEAD'),'2')
        self.assertEqual(len(self.client.posts),2)

    def test_repeated_verified_caller_is_addressed_once(self):
        row=self.host.records('documents','dingTalkDispatch','fixture-dispatch');p=row['payload']
        p['verifiedMessages'].append({**p['verifiedMessages'][0],'messageId':'other-original-message'})
        self.host.put('documents','dingTalkDispatch','fixture-dispatch',p,expected_revision=row['revision'])
        result=self.run_job(self.inspect())
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.robot.last_users,['owner'])
        self.assertEqual(len([c for c in result['notification_coverage'] if c['role']=='caller']),1)

    def test_task_receipt_conflict_is_retried_locally_without_resending(self):
        put=self.host.put;failures=[]
        def conflict(table,service,record_id,payload,expected_revision=None):
            if table=='tasks' and 'notificationCoverage' in payload and not failures:
                failures.append(1);raise ValueError('revision conflict')
            return put(table,service,record_id,payload,expected_revision)
        self.host.put=conflict
        result=self.run_job(self.inspect())
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(self.robot.sent),1)

    def test_same_configured_owner_in_both_notification_roles_is_addressed_once(self):
        inspected=self.inspect()
        pipeline=Pipeline(database=self.root/'tool.sqlite3',output_root=self.root/'artifacts',host=self.host,robot=self.robot,client=self.client)
        plan=json.loads(pipeline.read('fixture-delivery')['plan'])
        plan['selected'][0]['creator']='repairer'
        self.client.bug.update(status='resolved',resolution='fixed',assignedTo='repairer',openedBy='repairer')
        data={'commit':self.git('rev-parse','HEAD')}
        pipeline.notify_result('fixture-delivery',plan,data)
        self.assertEqual(self.robot.targets[0]['users'],['owner'])
        self.assertEqual(self.robot.sent[0][1],[])
        self.assertEqual({c['role'] for c in data['notification_coverage']},{'caller','creator'})

    def test_desktop_caller_is_returned_without_inventing_a_dingtalk_identity(self):
        self.manifest.pop('source_dispatch_id')
        result=self.run_job(self.inspect())
        self.assertEqual(result['status'],'completed',result)
        caller=next(c for c in result['notification_coverage'] if c['role']=='caller')
        self.assertEqual((caller['channel'],caller['status']),('tool_return','returned'))
        self.assertEqual(self.robot.targets[0]['users'],[])


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.robot=Robot({'profile':'fixed-profile','allowedGroupId':'fixed-group','ownerStaffId':'fixed-owner','robotCode':'fixed-bot'})

    def test_group_and_owner_notification_never_change_identity_or_organization(self):
        receipts=[{'ok':True,'failedCount':0,'succeededCount':1,'succeeded':[{'target':'fixed-group','result':{'success':True}}]},
                  {'ok':True,'result':{'success':True,'result':{'invalidStaffIdList':[],'flowControlledStaffIdList':[]}}}]
        def answer(command, **options):
            body=options['input'].decode('utf-8')
            r={'dry_run':True,'executed':False,'body':body} if '--dry-run' in command else receipts.pop(0)
            return SimpleNamespace(returncode=0,stdout=json.dumps(r).encode(),stderr=b'')
        with patch('bugfixer.delivery_host.subprocess.run',side_effect=answer) as run:
            self.robot.send('已核验交付',['known-member'])
            self.robot.send('需要原主人确认',owner_dm=True)
        for call in run.call_args_list:
            command=call.args[0]
            self.assertEqual(command[command.index('--as')+1],'bot')
            self.assertEqual(command[command.index('--profile')+1],'fixed-profile')
        self.assertIn('--groups',run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[3].args[0][run.call_args_list[3].args[0].index('--users')+1],'fixed-owner')
        self.assertTrue(all(isinstance(c.kwargs['input'],bytes) and b'\r' not in c.kwargs['input'] for c in run.call_args_list))

    def test_vendor_error_code_is_reported_without_private_diagnostics_or_retry(self):
        detail={'error':{'technical_detail':'{"dingOpenErrcode":480101,"password":"private-secret"}'}}
        response=SimpleNamespace(returncode=1,stdout='',stderr=json.dumps(detail))
        preview=SimpleNamespace(returncode=0,stdout=json.dumps({'dry_run':True,'executed':False,'body':'交付'}).encode(),stderr=b'')
        with patch('bugfixer.delivery_host.subprocess.run',side_effect=[preview,response]) as run:
            with self.assertRaises(BugFixerError) as error:self.robot.send('交付')
        self.assertEqual(error.exception.code,'dws_rejected_480101')
        self.assertNotIn('private-secret',error.exception.message)
        self.assertEqual(run.call_count,2)

    def test_incomplete_group_membership_cannot_be_treated_as_verified(self):
        with patch.object(self.robot,'call',return_value={'complete':False,'hasMore':True,'users':[]}):
            with self.assertRaises(BugFixerError) as error:self.robot.members()
        self.assertEqual(error.exception.code,'members_incomplete')

    def test_delivery_ledger_must_match_the_authorized_group(self):
        receipt={'ok':True,'failedCount':0,'succeededCount':1,'succeeded':[{'target':'different-group','result':{'success':True}}]}
        with patch.object(self.robot,'call',side_effect=[{'dry_run':True,'executed':False,'body':'交付'},receipt]):
            with self.assertRaises(BugFixerError) as error:self.robot.send('交付')
        self.assertEqual(error.exception.code,'dws_uncertain')

    def test_preview_mismatch_prevents_service_send(self):
        with patch.object(self.robot,'call',return_value={'dry_run':True,'executed':False,'body':'乱码'}) as call:
            with self.assertRaises(BugFixerError) as error:self.robot.send('中文原文\n第二行')
        self.assertEqual(error.exception.code,'dws_before_send');self.assertEqual(call.call_count,1)

    def test_real_subprocess_keeps_chinese_lf_bytes_and_both_address_types(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'dws_fixture.py'
            script.write_text('''import json,sys
body=sys.stdin.buffer.read()
assert b"\\r" not in body
text=body.decode("utf-8")
assert "中文正文\\n第二行" == text
args=sys.argv[1:]
assert args[args.index("--as")+1]=="bot"
assert args[args.index("--at-user-ids")+1]=="caller"
assert args[args.index("--at-open-dingtalk-ids")+1]=="creator"
result={"dry_run":True,"executed":False,"body":text} if "--dry-run" in args else {"ok":True,"succeededCount":1,"failedCount":0,"succeeded":[{"target":"fixed-group","result":{"success":True}}]}
print(json.dumps(result))
''',encoding='utf-8')
            robot=Robot(self.robot.config,command=[sys.executable,str(script)])
            self.assertTrue(robot.send('中文正文\r\n第二行',['creator'],user_ids=['caller'])['ok'])

    def test_full_body_readback_requires_exact_group_robot_and_unique_message(self):
        good={'openConversationId':'fixed-group','sendType':'robot','content':'中文正文 第二行','openMessageId':'actual-message'}
        with patch.object(self.robot,'call',return_value={'messages':[good]}):
            self.assertEqual(self.robot.verify('中文正文\n第二行','2026-11-25T00:00:00Z')['message_id'],'actual-message')
        with patch.object(self.robot,'call',return_value={'messages':[good]}) as call:
            self.robot.verify('中文正文\n第二行','2026-11-25T00:00:00Z')
            args=call.call_args.args[0]
            self.assertEqual(args[:3],['chat','message','list'])
            self.assertEqual(args[args.index('--time')+1],'2026-11-25 08:00:00')
        for messages in ([{**good,'openConversationId':'other-group'}],[{**good,'sendType':'user'}],[{**good,'content':'中文正文'}],[good,good]):
            with patch.object(self.robot,'call',return_value={'messages':messages}):
                with self.assertRaises(BugFixerError):self.robot.verify('中文正文\n第二行','2026-11-25T00:00:00Z')


if __name__ == '__main__':
    unittest.main()
