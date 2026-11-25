import copy
import html
import http.server
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from bugfixer.common import BugFixerError, Credential
from bugfixer.http import Client, Response
from bugfixer.mcp import handle
from bugfixer.writeback import Form, prepare_writeback, apply_writeback, store


def response(data):
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    return Response('http://zen.wckj.com/', 200, {'Content-Type': 'text/html; charset=UTF-8'}, data.encode())


class FakeWriteZen:
    def __init__(self):
        self.bug = {'id': '12', 'product': '4', 'title': 'Synthetic repair', 'status': 'active',
                    'assignedTo': 'repairer', 'openedBy': 'tester', 'steps': '<p>原始步骤</p><img src="/file-read-1.png" />',
                    'lastEditedDate': '2026-11-24 10:00:00', 'pri': '3', 'openedBuild': 'trunk,155',
                    'files': {'1': {'id': '1', 'title': '原图'}}, 'confirmed': '0'}
        self.actions = [{'id': '1', 'actor': 'tester', 'action': 'opened'}]
        self.posts = []; self.gets = []; self.fail_after = None; self.drop_steps = False
        self.transfer_before_resolve = False
        self.inject_view_handler = False
        self.drop_comment = False
        self.extra_control = False
        self.confirm_during_edit = False

    def get(self, url):
        self.gets.append(url)
        if url.endswith('.json'):
            if 'bug-resolve-' in url and self.posts and self.transfer_before_resolve:
                self.bug['assignedTo'] = 'other'
            bug = copy.deepcopy(self.bug)
            if self.inject_view_handler and 'bug-view-' in url:
                bug['steps'] = bug['steps'].replace('<img ', '<img onload="setImageSize(this,0)" ')
            return response({'status': 'success', 'data': json.dumps({'bug': bug, 'actions': self.actions,
                             'users': {'repairer': '维修', 'tester': '测试'}, 'builds': {'trunk': '主干', '155': '6.6.0'}})})
        if 'bug-view-' in url:
            return response('<img src="/file-read-1.png" />')
        if 'bug-edit-' in url:
            return response("<form method='post' id='dataform'>"
                + ''.join(f'<input name="{k}" value="{html.escape(str(self.bug[k]), quote=True)}" />'
                          for k in ('title', 'status', 'assignedTo', 'lastEditedDate', 'pri', 'product'))
                + '<textarea name="steps">' + html.escape(self.bug['steps']) + '</textarea>'
                + '<textarea name="comment"></textarea><input type="file" name="files[]" />'
                + '<select name="openedBuild[]" multiple><option selected value="trunk">主干</option>'
                + '<option selected value="155">版本</option></select>'
                + ('<input name="deleteFiles" value="1" />' if self.extra_control else '') + '</form>')
        return response("<form method='post'><select name='resolution'><option value='fixed'>已解决</option></select>"
                        "<select name='resolvedBuild'><option value=''>请选择</option></select>"
                        "<select name='assignedTo'><option value='tester'>测试</option></select>"
                        "<input type='hidden' name='status' value='resolved' /><input type='checkbox' name='createBuild' value='1' />"
                        "<textarea name='comment'></textarea></form>")

    def post_write_form(self, url, values):
        self.posts.append((url, copy.deepcopy(values)))
        action = 'edit' if 'bug-edit-' in url else 'resolve'
        if action == 'edit':
            if not self.drop_steps:
                self.bug['steps'] = values['steps']
            if self.confirm_during_edit:
                self.bug['confirmed'] = '1'
        else:
            for key in ('status', 'resolution', 'resolvedBuild', 'assignedTo'):
                self.bug[key] = values[key]
            self.bug['confirmed'] = '1'
        self.bug['lastEditedDate'] = f'2026-11-24 10:00:0{len(self.posts)}'
        self.actions.append({'id': str(len(self.actions) + 1), 'actor': 'repairer', 'action': action,
                             'comment': '' if self.drop_comment else values.get('comment', '')})
        if self.fail_after == action:
            raise BugFixerError('network_error', 'Fixture timeout after server commit.', True)
        return response({'result': 'success'})


class WritebackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = self.root / 'store.sqlite3'; self.client = FakeWriteZen()
        self.credential = Credential('repairer', 'fixture-secret-password')
        self.auth = patch('bugfixer.writeback._credential', side_effect=lambda *_: self.credential)
        self.auth.start(); self.addCleanup(self.auth.stop); self.addCleanup(self.temp.cleanup)
        self.request = {'request_id': 'bug12-delivery-1', 'id': '12', 'credential_ref': 'fixture', 'task_mode': 'repair',
                        'expected_assigned_to': 'repairer', 'assigned_to': 'tester', 'resolved_build': 'trunk',
                        'reason': '原因<&>', 'change': '调整布局', 'acceptance': '请检查完整显示',
                        'branch': 'dev', 'commit': 'a' * 40}

    def prepare(self, **overrides):
        return prepare_writeback({**self.request, **overrides}, database=self.db, client=self.client)

    def apply(self, preview):
        return apply_writeback({k: preview[k] for k in ('request_id', 'preview_hash')}, database=self.db,
                               client=self.client, output_root=self.root / 'artifacts')

    def test_prepare_is_read_only_and_then_exact_delivery_preserves_original(self):
        original = copy.deepcopy(self.client.bug)
        preview = self.prepare()
        self.assertEqual(preview['status'], 'prepared', preview)
        self.assertEqual(self.client.posts, [])
        result = self.apply(preview)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(self.client.posts), 2)
        self.assertTrue(self.client.bug['steps'].startswith(original['steps']))
        self.assertIn('原因&lt;&amp;&gt;', self.client.bug['steps'])
        self.assertEqual(self.client.bug['files'], original['files'])
        self.assertEqual(self.client.posts[0][1]['openedBuild[]'], ['trunk', '155'])
        self.assertNotIn('files[]', self.client.posts[0][1])
        self.assertNotIn('createBuild', self.client.posts[1][1])
        self.assertEqual(self.client.bug['status'], 'resolved')
        self.assertEqual(self.client.bug['assignedTo'], 'tester')
        self.assertTrue(Path(result['readback_path']).is_file())
        self.assertNotIn(self.credential.password, self.db.read_bytes().decode('utf-8', errors='ignore'))

    def test_duplicate_apply_and_prepare_do_not_replay_posts(self):
        preview = self.prepare(); self.apply(preview)
        self.assertTrue(self.apply(preview)['deduplicated'])
        self.assertEqual(self.prepare()['status'], 'completed')
        self.assertEqual(len(self.client.posts), 2)

    def test_resolve_may_confirm_the_bug_as_observed_on_the_live_service(self):
        self.assertEqual(self.client.bug['confirmed'], '0')
        result = self.apply(self.prepare())
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.client.bug['confirmed'], '1')

    def test_confirmation_change_during_edit_is_still_rejected(self):
        self.client.confirm_during_edit = True
        result = self.apply(self.prepare())
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['error']['code'], 'readback_mismatch')
        self.assertEqual(len(self.client.posts), 1)

    def test_view_only_image_handler_is_not_a_conflict_or_written_back(self):
        self.client.inject_view_handler = True
        preview = self.prepare()
        self.assertEqual(preview['status'], 'prepared', preview)
        result = self.apply(preview)
        self.assertEqual(result['status'], 'completed', result)
        self.assertNotIn('setImageSize', self.client.posts[0][1]['steps'])
        self.assertIn('原因&lt;&amp;&gt;', self.client.posts[1][1]['comment'])

    def test_reused_request_id_cannot_change_content_or_account(self):
        self.prepare()
        self.assertEqual(self.prepare(reason='different')['error']['code'], 'request_conflict')
        self.credential = Credential('someone-else', 'fixture-secret-password')
        self.assertEqual(self.prepare()['error']['code'], 'request_conflict')
        self.assertEqual(self.client.posts, [])

    def test_apply_requires_exact_preview_and_credential_identity(self):
        preview = self.prepare()
        result = apply_writeback({'request_id': preview['request_id'], 'preview_hash': 'bad'}, database=self.db, client=self.client)
        self.assertEqual(result['error']['code'], 'preview_conflict')
        self.credential = Credential('other', 'fixture-secret-password')
        self.assertEqual(self.apply(preview)['error']['code'], 'credential_scope_mismatch')
        self.assertEqual(self.client.posts, [])

    def test_changed_saved_plan_cannot_reuse_the_original_preview_hash(self):
        preview = self.prepare()
        with store(self.db) as conn:
            row = conn.execute('SELECT plan_json FROM writeback_requests WHERE request_id=?', (preview['request_id'],)).fetchone()
            plan = json.loads(row[0]); plan['request']['reason'] = 'changed after preview'
            conn.execute('UPDATE writeback_requests SET plan_json=? WHERE request_id=?', (json.dumps(plan), preview['request_id']))
        self.assertEqual(self.apply(preview)['error']['code'], 'journal_error')
        self.assertEqual(self.client.posts, [])

    def test_concurrent_changes_to_description_assignee_or_history_prevent_writes(self):
        for field in ('steps', 'assignedTo', 'history'):
            with self.subTest(field=field):
                self.client = FakeWriteZen()
                preview = self.prepare(request_id='conflict-' + field)
                if field == 'history':
                    self.client.actions.append({'action': 'comment', 'comment': 'another person'})
                else:
                    self.client.bug[field] = 'changed'
                self.assertEqual(self.apply(preview)['error']['code'], 'state_conflict')
                self.assertEqual(self.client.posts, [])

    def test_invalid_mode_version_assignee_and_secret_report_are_rejected(self):
        for changes, code in [({'task_mode': 'analysis'}, 'invalid_request'),
                              ({'resolved_build': 'nonexistent'}, 'invalid_build'),
                              ({'assigned_to': 'closed'}, 'invalid_assignee'),
                              ({'reason': self.credential.password}, 'secret_in_report')]:
            self.assertEqual(self.prepare(**changes)['error']['code'], code)
        self.assertEqual(self.client.posts, [])

    def test_lost_description_stops_before_resolve(self):
        self.client.drop_steps = True
        result = self.apply(self.prepare())
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['error']['code'], 'readback_mismatch')
        self.assertEqual(len(self.client.posts), 1)

    def test_missing_resolve_comment_is_not_reported_as_complete(self):
        self.client.drop_comment = True
        preview = self.prepare()
        self.assertEqual(self.apply(preview)['status'], 'uncertain')
        self.assertEqual(self.apply(preview)['status'], 'uncertain')
        self.assertEqual(len(self.client.posts), 2)

    def test_unexpected_form_controls_are_not_silently_posted(self):
        self.client.extra_control = True
        self.assertEqual(self.prepare()['error']['code'], 'contract_changed')
        self.assertEqual(self.client.posts, [])

    def test_source_login_secret_is_not_persisted_in_plan(self):
        self.client.bug['steps'] += self.credential.password
        self.assertEqual(self.prepare()['error']['code'], 'secret_in_context')
        self.assertFalse(self.db.exists() and self.credential.password.encode() in self.db.read_bytes())

    def test_timeout_after_edit_is_not_automatically_retried(self):
        self.client.fail_after = 'edit'
        preview = self.prepare()
        self.assertEqual(self.apply(preview)['status'], 'uncertain')
        self.assertEqual(self.apply(preview)['status'], 'uncertain')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.bug['status'], 'active')

    def test_timeout_after_resolve_can_be_reconciled_read_only(self):
        self.client.fail_after = 'resolve'; preview = self.prepare()
        self.assertEqual(self.apply(preview)['status'], 'uncertain')
        result = self.apply(preview)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['reconciled'])
        self.assertFalse(result['business_writes_this_call'])
        self.assertEqual(len(self.client.posts), 2)

    def test_transfer_between_edit_and_resolve_stops_second_post(self):
        self.client.transfer_before_resolve = True
        result = self.apply(self.prepare())
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.client.posts), 1)

    def test_other_uncertain_delivery_blocks_another_request_for_same_bug(self):
        preview = self.prepare()
        with store(self.db) as conn:
            conn.execute("UPDATE writeback_requests SET state='uncertain' WHERE request_id=?", (preview['request_id'],))
        other = self.prepare(request_id='other-request')
        self.assertEqual(self.apply(other)['error']['code'], 'write_in_progress')
        self.assertEqual(self.client.posts, [])

    def test_http_write_route_is_narrow_and_post_never_retries(self):
        client = Client(retries=5)
        for url in ('http://zen.wckj.com/bug-delete-12.html', 'https://evil/bug-edit-12.html',
                    'http://zen.wckj.com/bug-edit-12.html?delete=1'):
            with self.assertRaises(BugFixerError):
                client.post_write_form(url, {})
        with patch.object(client.opener, 'open', side_effect=urllib.error.URLError('timeout')) as opened:
            with self.assertRaises(BugFixerError):
                client.post_write_form('http://zen.wckj.com/bug-edit-12.html', {'steps': 'report'})
            self.assertEqual(opened.call_count, 1)

    def test_form_has_no_files_or_unchecked_checkbox_and_preserves_multi_select(self):
        form = Form(self.client.get('http://zen.wckj.com/bug-edit-12.html').body.decode())
        self.assertEqual(form.values['openedBuild[]'], ['trunk', '155'])
        self.assertNotIn('files[]', form.values)
        form = Form(self.client.get('http://zen.wckj.com/bug-resolve-12.html').body.decode())
        self.assertNotIn('createBuild', form.values)

    def test_mcp_apply_dispatch_and_uncertainty_are_explicit(self):
        with patch('bugfixer.mcp.apply_writeback', return_value={'status': 'uncertain'}) as operation:
            result = handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'apply_writeback', 'arguments': {'request_id': 'x', 'preview_hash': 'y'}}})
            self.assertTrue(result['result']['isError'])
            operation.assert_called_once_with({'request_id': 'x', 'preview_hash': 'y'})

    def test_real_tcp_form_encoding_and_remote_readback(self):
        backend = self.client
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def send(self, result):
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=UTF-8')
                self.send_header('Content-Length', str(len(result.body))); self.end_headers(); self.wfile.write(result.body)
            def do_GET(self):
                self.send(backend.get('http://zen.wckj.com' + self.path))
            def do_POST(self):
                values = urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode(), keep_blank_values=True)
                values = {k: v if k.endswith('[]') else v[0] for k, v in values.items()}
                self.send(backend.post_write_form('http://zen.wckj.com' + self.path, values))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = Client(retries=0)
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def routed(request, timeout):
            url = f'http://127.0.0.1:{server.server_port}' + urllib.parse.urlsplit(request.full_url).path
            return local.open(urllib.request.Request(url, data=request.data, headers=dict(request.header_items())), timeout=timeout)
        try:
            with patch.object(client.opener, 'open', side_effect=routed):
                preview = prepare_writeback(self.request, database=self.db, client=client)
                result = apply_writeback({k: preview[k] for k in ('request_id', 'preview_hash')},
                                         database=self.db, client=client, output_root=self.root / 'http-artifacts')
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(backend.posts[0][1]['openedBuild[]'], ['trunk', '155'])
            self.assertIn('修改：调整布局', backend.posts[1][1]['comment'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_receipt_failure_does_not_claim_completed_or_retry_post(self):
        preview = self.prepare()
        with patch('bugfixer.writeback._save', return_value=False):
            result = self.apply(preview)
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.apply(preview)['status'], 'completed')
        self.assertEqual(len(self.client.posts), 2)


if __name__ == '__main__':
    unittest.main()
