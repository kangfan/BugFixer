"""Prepared ZenTao repair delivery, optimistic conflict checks and durable deduplication."""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

from .artifacts import now, write_json
from .common import BugFixerError, decode_text, html_text, redact
from .credentials import connect, resolve
from .http import Client
from .service import DEFAULT_DATABASE, DEFAULT_OUTPUT
from .zentao import BASE, fetch, unwrap

STABLE = ('id', 'product', 'branch', 'module', 'project', 'plan', 'story', 'task', 'title',
          'keywords', 'severity', 'pri', 'type', 'os', 'browser', 'hardware', 'found',
          'confirmed', 'openedBy', 'openedDate', 'openedBuild', 'deadline', 'mailto', 'files',
          'closedBy', 'closedDate', 'duplicateBug', 'deleted')
MUTABLE = ('steps', 'status', 'assignedTo', 'assignedDate', 'lastEditedBy', 'lastEditedDate',
           'resolution', 'resolvedBuild', 'resolvedBy', 'resolvedDate')
EDIT_FIELDS = frozenset(('color', 'title', 'steps', 'comment', 'labels[]', 'lastEditedDate', 'product',
    'module', 'plan', 'type', 'severity', 'pri', 'status', 'assignedTo', 'deadline', 'os', 'browser',
    'keywords', 'mailto[]', 'project', 'task', 'openedBuild[]', 'resolvedBy', 'resolvedDate',
    'resolvedBuild', 'resolution', 'duplicateBug', 'closedBy', 'closedDate'))
RESOLVE_FIELDS = frozenset(('resolution', 'duplicateBug', 'buildProject', 'resolvedBuild', 'buildName',
    'createBuild', 'resolvedDate', 'assignedTo', 'status', 'labels[]', 'comment'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def canonical_steps(value):
    # ZenTao view injects this presentation-only handler; edit returns the stored HTML.
    return re.sub(r'''\s+onload=(["'])setImageSize\(this,\s*0\)\1''', '', value or '').replace('\r\n', '\n')


def snapshot(context):
    bug = {k: context['bug'].get(k) for k in STABLE + MUTABLE}
    bug['steps'] = canonical_steps(bug['steps'])
    return digest({'bug': bug, 'actions': context['actions']})


class Form(HTMLParser):
    """Successful controls of the single business form, without files or scripts."""
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.forms = []; self.form = None; self.select = None; self.option = None; self.textarea = None
        self.feed(page)
        if len(self.forms) != 1:
            raise BugFixerError('contract_changed', 'Expected one ZenTao business form.')
        self.values, self.options = self.forms[0]['values'], self.forms[0]['options']

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            if self.form is not None or a.get('method', '').lower() != 'post' or a.get('action'):
                raise BugFixerError('contract_changed', 'Unrecognized ZenTao form target.')
            self.form = {'values': {}, 'options': {}}; self.forms.append(self.form)
        if self.form is None:
            return
        name = a.get('name')
        if tag == 'input' and name and 'disabled' not in a:
            kind = a.get('type', 'text').lower()
            if kind not in ('file', 'submit', 'button', 'reset', 'image') and (kind not in ('checkbox', 'radio') or 'checked' in a):
                self.form['values'][name] = a.get('value', '')
        elif tag == 'textarea' and name and 'disabled' not in a:
            self.textarea = name; self.form['values'][name] = ''
        elif tag == 'select' and name and 'disabled' not in a:
            self.select = {'name': name, 'multiple': 'multiple' in a, 'items': []}
        elif tag == 'option' and self.select is not None:
            self.option = {**a, 'text': ''}

    def handle_data(self, text):
        if self.form is not None and self.textarea:
            self.form['values'][self.textarea] += text
        if self.option is not None:
            self.option['text'] += text

    def handle_endtag(self, tag):
        if tag == 'textarea':
            self.textarea = None
        elif tag == 'option' and self.option is not None:
            self.select['items'].append(self.option); self.option = None
        elif tag == 'select' and self.select is not None:
            name, items = self.select['name'], [o for o in self.select['items'] if 'disabled' not in o]
            values = [o.get('value', o['text']) for o in items if 'selected' in o]
            self.form['options'][name] = {o.get('value', o['text']): o['text'] for o in items}
            if not self.select['multiple']:
                self.form['values'][name] = values[0] if values else (items[0].get('value', items[0]['text']) if items else '')
            elif values:
                self.form['values'][name] = values
            self.select = None
        elif tag == 'form':
            self.form = None


@contextmanager
def store(database):
    with connect(Path(database)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS writeback_requests (
            request_id TEXT PRIMARY KEY, bug_id TEXT NOT NULL, account TEXT NOT NULL,
            payload_hash TEXT NOT NULL, plan_json TEXT NOT NULL, state TEXT NOT NULL,
            result_json TEXT, updated_at TEXT NOT NULL)''')
        yield conn


def _error(error, **extra):
    return {'status': 'blocked' if error.code in ('credentials_required', 'authentication_required',
            'permission_denied', 'authentication_failed', 'browser_fallback_required') else 'error',
            'error': {'code': error.code, 'message': error.message}, **extra}


def _validate(request):
    required = {'request_id', 'id', 'credential_ref', 'task_mode', 'expected_assigned_to',
                'assigned_to', 'resolved_build', 'reason', 'change', 'acceptance', 'branch', 'commit'}
    if not isinstance(request, dict) or set(request) != required or any(not isinstance(v, str) or not v.strip() or len(v) > 5000 for v in request.values()):
        raise BugFixerError('invalid_request', 'Supply exactly the documented nonempty string fields.')
    if (request['task_mode'] != 'repair' or not re.fullmatch(r'[1-9][0-9]{0,11}', request['id'])
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request['request_id'])
            or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', request['commit'])):
        raise BugFixerError('invalid_request', 'Repair mode, numeric Bug ID, simple request ID and full commit SHA are required.')
    return dict(request)


def _credential(ref, database):
    if not isinstance(ref, str):
        raise BugFixerError('invalid_request', 'A named local ZenTao credential is required.')
    return resolve({'source': 'zentao', 'credential_ref': ref}, Path(database))


def _fresh(client, bug_id, credential):
    return fetch(client, {'id': bug_id}, credential)['context']


def _active(context, expected):
    if context['bug'].get('deleted') not in (None, '0', 0, False) or context['status'] != 'active' or context['assigned_to'] != expected:
        raise BugFixerError('state_conflict', 'Bug is no longer active and assigned to the expected repair account.')


def _forms(client, bug_id, context):
    forms = {}
    for action in ('edit', 'resolve'):
        data = unwrap(client.get(BASE + f'/bug-{action}-{bug_id}.json'))
        actions = data.get('actions', [])
        current = {'bug': data.get('bug', {}), 'actions': list(actions.values()) if isinstance(actions, dict) else actions}
        if snapshot(current) != snapshot(context):
            raise BugFixerError('state_conflict', 'Bug changed while reading its write forms.')
        page = decode_text(client.get(BASE + f'/bug-{action}-{bug_id}.html').body)
        forms[action] = Form(page)
        if action == 'resolve':
            forms['builds'] = data.get('builds', {})
    if not {'steps', 'lastEditedDate', 'assignedTo', 'status', 'title'} <= forms['edit'].values.keys():
        raise BugFixerError('contract_changed', 'The edit form is missing required preservation/conflict fields.')
    if not {'resolution', 'resolvedBuild', 'assignedTo', 'status', 'comment'} <= forms['resolve'].values.keys():
        raise BugFixerError('contract_changed', 'The resolve form is missing required fields.')
    if set(forms['edit'].values) - EDIT_FIELDS or set(forms['resolve'].values) - RESOLVE_FIELDS:
        raise BugFixerError('contract_changed', 'Unexpected business-form controls require contract review.')
    if canonical_steps(forms['edit'].values['steps']) != canonical_steps(context['bug'].get('steps', '')):
        raise BugFixerError('state_conflict', 'Description differs between the form and current Bug.')
    bug, values = context['bug'], forms['edit'].values
    for name in ('title', 'status', 'assignedTo', 'lastEditedDate', 'type', 'os', 'browser', 'keywords'):
        if name in values and values[name] != str(bug.get(name) or ''):
            raise BugFixerError('state_conflict', 'An edit-form value differs from the current Bug.')
    for name in ('product', 'module', 'project', 'plan', 'task', 'severity', 'pri'):
        if name in values and str(values[name] or '0') != str(bug.get(name) or '0'):
            raise BugFixerError('state_conflict', 'An edit-form selection would change an unrelated field.')
    for name in ('openedBuild', 'mailto'):
        selected = values.get(name + '[]', [])
        selected = selected if isinstance(selected, list) else [selected]
        if sorted(x for x in selected if x) != sorted(x for x in str(bug.get(name) or '').split(',') if x):
            raise BugFixerError('state_conflict', 'An edit-form multiple selection would change an unrelated field.')
    return forms


def _targets(forms, request):
    if request['resolved_build'] not in forms['builds'] or not request['resolved_build']:
        raise BugFixerError('invalid_build', 'Choose an existing resolved-build value; no version is created or guessed.')
    if (request['assigned_to'] not in forms['resolve'].options.get('assignedTo', {})
            or request['assigned_to'] in ('', 'closed', request['expected_assigned_to'])):
        raise BugFixerError('invalid_assignee', 'Choose a real testing account different from the repair assignee.')
    if 'fixed' not in forms['resolve'].options.get('resolution', {}):
        raise BugFixerError('contract_changed', 'The deployed form does not offer the fixed resolution.')


def _report(request):
    return '\n'.join(f'{label}：{request[key]}' for key, label in
                     [('reason', '原因'), ('change', '修改'), ('acceptance', '验收'), ('branch', '分支'), ('commit', '提交')])


def _report_html(request):
    return ''.join('<p>' + html.escape(line) + '</p>' for line in _report(request).splitlines())


def _preview(plan):
    r = plan['request']
    return {'status': 'prepared', 'request_id': r['request_id'], 'preview_hash': plan['preview_hash'],
            'preview': {'id': r['id'], 'title': plan['title'], 'description_append': _report(r),
                        'status': 'resolved', 'resolution': 'fixed', 'assigned_to': r['assigned_to'],
                        'resolved_build': r['resolved_build'], 'resolved_build_label': plan['build_label'],
                        'original_description_preserved': True}, 'business_writes': False}


def prepare_writeback(request, *, database=DEFAULT_DATABASE, client=None):
    try:
        request = _validate(request)
        credential = _credential(request['credential_ref'], database)
        # Never persist a submitted secret repeated inside a report field.
        if any(credential.password and credential.password in v for v in request.values()):
            raise BugFixerError('secret_in_report', 'A report field contains a credential; remove it before preparing.')
        payload_hash = digest(request)
        with store(database) as conn:
            row = conn.execute('SELECT * FROM writeback_requests WHERE request_id=?', (request['request_id'],)).fetchone()
        if row:
            if row['payload_hash'] != payload_hash or row['account'] != credential.account:
                raise BugFixerError('request_conflict', 'The request ID already belongs to different content or credentials.')
            plan = json.loads(row['plan_json'])
            return _preview(plan) if row['state'] == 'prepared' else json.loads(row['result_json'] or '{"status":"running"}')
        client = client or Client()
        context = _fresh(client, request['id'], credential)
        if credential.password and credential.password in json.dumps(context, ensure_ascii=False):
            raise BugFixerError('secret_in_context', 'The source context contains the login secret; it cannot be persisted in a write plan.')
        _active(context, request['expected_assigned_to'])
        forms = _forms(client, request['id'], context)
        _targets(forms, request)
        fragment = '<hr />' + _report_html(request)
        original = forms['edit'].values['steps']
        plan = {'request': request, 'snapshot': snapshot(context), 'title': context['title'],
                'original_steps': original, 'steps': original if original.endswith(fragment) else original + fragment,
                'stable': {k: context['bug'].get(k) for k in STABLE},
                'build_label': forms['builds'][request['resolved_build']]}
        plan['preview_hash'] = digest(plan)
        with store(database) as conn:
            conn.execute('INSERT INTO writeback_requests VALUES(?,?,?,?,?,?,?,?)',
                         (request['request_id'], request['id'], credential.account, payload_hash,
                          json.dumps(plan, ensure_ascii=False), 'prepared', None, now()))
        return _preview(plan)
    except BugFixerError as e:
        return _error(e, business_writes=False)
    except sqlite3.IntegrityError:
        return _error(BugFixerError('request_conflict', 'A concurrent request prepared this ID; read it again.'), business_writes=False)
    except (OSError, ValueError, KeyError, TypeError):
        return _error(BugFixerError('prepare_error', 'Could not prepare a complete writeback preview.'), business_writes=False)
    except sqlite3.Error:
        return _error(BugFixerError('journal_error', 'The local writeback journal is unavailable.'), business_writes=False)


def _preserved(context, plan, *, resolved=False):
    bug = context['bug']
    for key, expected in plan['stable'].items():
        actual = bug.get(key)
        # Deployed resolve confirms an unconfirmed Bug; edit must not do this.
        if (resolved and key == 'confirmed' and str(expected) == '0' and str(actual) == '1'
                and bug.get('status') == 'resolved' and bug.get('resolution') == 'fixed'):
            continue
        if actual != expected:
            return False
    return True


def _done(context, plan):
    r = plan['request']; bug = context['bug']
    comment_saved = any(' '.join(html_text(str(a.get('comment', ''))).split()) == ' '.join(_report(r).split())
                        for a in context['actions'] if isinstance(a, dict) and a.get('action') in ('resolve', 'resolved'))
    return (_preserved(context, plan, resolved=True) and canonical_steps(bug.get('steps')) == canonical_steps(plan['steps']) and bug.get('status') == 'resolved'
            and bug.get('resolution') == 'fixed' and bug.get('assignedTo') == r['assigned_to']
            and str(bug.get('resolvedBuild')) == r['resolved_build'] and comment_saved)


def _save(database, request_id, state, result):
    try:
        with store(database) as conn:
            conn.execute('UPDATE writeback_requests SET state=?,result_json=?,updated_at=? WHERE request_id=?',
                         (state, json.dumps(result, ensure_ascii=False), now(), request_id))
        return True
    except (sqlite3.Error, OSError):
        return False


def apply_writeback(request, *, database=DEFAULT_DATABASE, output_root=DEFAULT_OUTPUT, client=None):
    request_id = None; claimed = False; attempted = False; plan = None; credential = None
    try:
        if (not isinstance(request, dict) or set(request) != {'request_id', 'preview_hash'}
                or not all(isinstance(v, str) for v in request.values())):
            raise BugFixerError('invalid_request', 'Apply accepts only request_id and the prepared preview_hash.')
        request_id = request['request_id']
        with store(database) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM writeback_requests WHERE request_id=?', (request_id,)).fetchone()
            if not row:
                raise BugFixerError('not_prepared', 'Prepare this request before applying it.')
            plan = json.loads(row['plan_json'])
            if digest({k: v for k, v in plan.items() if k != 'preview_hash'}) != plan.get('preview_hash'):
                raise BugFixerError('journal_error', 'The saved plan no longer matches its content hash.')
            if request['preview_hash'] != plan['preview_hash']:
                raise BugFixerError('preview_conflict', 'Apply must use the exact prepared preview hash.')
            credential = _credential(plan['request']['credential_ref'], database)
            if credential.account != row['account']:
                raise BugFixerError('credential_scope_mismatch', 'The named credential account changed since preparation.')
            if row['state'] in ('completed', 'conflict'):
                return {**json.loads(row['result_json']), 'deduplicated': True, 'business_writes_this_call': False}
            recover_only = row['state'] != 'prepared'
            if not recover_only:
                other = conn.execute("SELECT request_id FROM writeback_requests WHERE bug_id=? AND request_id<>? AND state IN ('running','uncertain')", (row['bug_id'], request_id)).fetchone()
                if other:
                    raise BugFixerError('write_in_progress', 'This Bug has another in-flight or uncertain write; reconcile it first.')
                conn.execute("UPDATE writeback_requests SET state='running',updated_at=? WHERE request_id=?", (now(), request_id))
                claimed = True
        client = client or Client()
        r = plan['request']
        context = _fresh(client, r['id'], credential)
        if recover_only:
            # A timed-out POST might have committed: reconcile by reading, never resend it.
            path = write_json(Path(output_root) / ('writeback-' + request_id) / ('reconcile-' + uuid.uuid4().hex[:12] + '.json'),
                              redact(context, (credential.password,)))
            if _done(context, plan):
                result = {'status': 'completed', 'request_id': request_id, 'id': r['id'],
                          'verified': True, 'reconciled': True, 'business_writes_this_call': False,
                          'bug_status': 'resolved', 'assigned_to': r['assigned_to'], 'readback_path': path, 'verified_at': now()}
                if not _save(database, request_id, 'completed', result):
                    result.update(status='uncertain', error={'code': 'journal_error', 'message': 'Remote readback matched but the durable receipt could not be saved.'})
                return result
            return {'status': 'uncertain', 'request_id': request_id, 'business_writes_this_call': False,
                    'readback_path': path, 'current_status': context['status'], 'current_assigned_to': context['assigned_to'],
                    'error': {'code': 'reconciliation_required', 'message': 'An earlier write may have run; no POST was repeated. Inspect current evidence before continuing.'}}
        _active(context, r['expected_assigned_to'])
        if snapshot(context) != plan['snapshot']:
            raise BugFixerError('state_conflict', 'Bug description, state, assignee or history changed after preparation.')
        forms = _forms(client, r['id'], context)
        _targets(forms, r)
        # Recheck immediately before the first POST; the form also carries lastEditedDate.
        if snapshot(_fresh(client, r['id'], credential)) != plan['snapshot']:
            raise BugFixerError('state_conflict', 'Bug changed immediately before the write.')
        run = Path(output_root) / ('writeback-' + request_id)
        write_json(run / 'before.json', redact(context, (credential.password,)))
        edit = dict(forms['edit'].values)
        edit.pop('labels[]', None)
        edit.update(steps=plan['steps'], comment='')
        attempted = True
        client.post_write_form(BASE + f"/bug-edit-{r['id']}.html", edit)
        context = _fresh(client, r['id'], credential)
        if not (_preserved(context, plan) and canonical_steps(context['bug'].get('steps')) == canonical_steps(plan['steps'])):
            raise BugFixerError('readback_mismatch', 'Description or original Bug fields were not preserved after edit.')
        write_json(run / 'after-edit.json', redact(context, (credential.password,)))
        _active(context, r['expected_assigned_to'])
        # Reload the resolve form and recheck state after the edit, not the old snapshot.
        forms = _forms(client, r['id'], context)
        _targets(forms, r)
        if snapshot(_fresh(client, r['id'], credential)) != snapshot(context):
            raise BugFixerError('state_conflict', 'Bug changed before resolving; the description may already be saved.')
        values = dict(forms['resolve'].values)
        for key in ('labels[]', 'createBuild', 'buildName'):
            values.pop(key, None)
        values.update(resolution='fixed', resolvedBuild=r['resolved_build'], assignedTo=r['assigned_to'],
                      status='resolved', comment=_report_html(r))
        client.post_write_form(BASE + f"/bug-resolve-{r['id']}.html", values)
        context = _fresh(client, r['id'], credential)
        path = write_json(run / 'readback.json', redact(context, (credential.password,)))
        if not _done(context, plan):
            raise BugFixerError('readback_mismatch', 'Final description, state, build or assignee did not match the prepared plan.')
        result = {'status': 'completed', 'request_id': request_id, 'id': r['id'], 'verified': True,
                  'bug_status': context['status'], 'assigned_to': context['assigned_to'],
                  'resolved_build': r['resolved_build'], 'readback_path': path, 'business_writes': True,
                  'business_writes_this_call': True, 'verified_at': now()}
        if not _save(database, request_id, 'completed', result):
            result.update(status='uncertain', error={'code': 'journal_error', 'message': 'Remote readback matched but the durable receipt could not be saved.'})
        return result
    except BugFixerError as e:
        result = _error(e, request_id=request_id, business_write_attempted=attempted)
        if claimed:
            result['status'] = 'uncertain' if attempted else 'blocked'
            _save(database, request_id, 'uncertain' if attempted else ('conflict' if e.code == 'state_conflict' else 'prepared'), result)
        return result
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        result = {'status': 'uncertain' if attempted else 'error', 'request_id': request_id,
                  'business_write_attempted': attempted, 'error': {'code': 'writeback_error', 'message': 'Writeback could not be fully verified; no write was retried.'}}
        if claimed:
            _save(database, request_id, 'uncertain' if attempted else 'prepared', result)
        return result


PREPARE_SCHEMA = {
    'name': 'prepare_writeback',
    'description': 'Prepare an already-authorized ZenTao repair delivery preview. Read-only remote; local durable plan. Does not grant approval or perform business writes.',
    'inputSchema': {'type': 'object', 'properties': {key: {'type': 'string'} for key in (
        'request_id', 'id', 'credential_ref', 'task_mode', 'expected_assigned_to', 'assigned_to',
        'resolved_build', 'reason', 'change', 'acceptance', 'branch', 'commit')},
        'required': ['request_id', 'id', 'credential_ref', 'task_mode', 'expected_assigned_to', 'assigned_to',
                     'resolved_build', 'reason', 'change', 'acceptance', 'branch', 'commit'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'openWorldHint': True},
}
PREPARE_SCHEMA['inputSchema']['properties']['task_mode'] = {'const': 'repair'}
APPLY_SCHEMA = {
    'name': 'apply_writeback',
    'description': 'Apply an exact prepared and already-authorized repair plan: append description, resolve fixed, assign to test and verify readback. A preview hash is not user approval. Never use in analysis mode.',
    'inputSchema': {'type': 'object', 'properties': {'request_id': {'type': 'string'}, 'preview_hash': {'type': 'string'}},
                    'required': ['request_id', 'preview_hash'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': True},
}
