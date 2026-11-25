"""Existing SQLite authority and DWS robot integration; no approval or identity switching."""
import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import BugFixerError

WORKSPACE_DB = Path('G:/okr/data/agent-workspace.sqlite3')


def workspace(database=WORKSPACE_DB):
    path = Path('G:/okr/integrations/agent-workspace/store.py')
    spec = importlib.util.spec_from_file_location('bugfixer_workspace_store', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.Store(database, profile='local')


class Robot:
    def __init__(self, config, *, command=None):
        self.config = config
        self.command = command or [str(Path.home()/'AppData/Roaming/npm/node_modules/dingtalk-workspace-cli/vendor/dws.exe')]

    def call(self, args, body=None):
        # Invoke the official packaged CLI directly: PowerShell pipes can recode stdin.
        command = [*self.command, *args,
                   '--profile', self.config['profile'], '--format', 'json']
        try:
            io = {'input': body.encode('utf-8')} if body is not None else {'stdin': subprocess.DEVNULL}
            p = subprocess.run(command, **io, capture_output=True, timeout=45,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise BugFixerError('dws_uncertain', 'DWS did not return a confirmed result; do not repeat a send.') from None
        if p.returncode:
            import re
            code = None
            try:
                error = json.loads(p.stderr or p.stdout).get('error', {})
                match = re.search(r'dingOpenErrcode[\\"\s:]+([0-9]+)', error.get('technical_detail', ''))
                code = match.group(1) if match else None
            except (ValueError, AttributeError, TypeError):
                pass
            raise BugFixerError('dws_rejected_' + code if code else 'dws_failed',
                                'DWS rejected the request; no resend or identity switch was attempted. Use DWS devdoc for the returned error code.')
        try:
            return json.loads(p.stdout)
        except ValueError:
            raise BugFixerError('dws_uncertain', 'DWS returned an unknown receipt; do not repeat a send.') from None

    def members(self):
        j = self.call(['chat', '+chat-members-list', '--conversation-id', self.config['allowedGroupId'],
                       '--member-types', 'user', '--page-limit', '5'])
        if j.get('complete') is not True or j.get('partial') or j.get('hasMore'):
            raise BugFixerError('members_incomplete', 'Original-group membership could not be fully verified.')
        return j.get('users', [])

    def preview(self, body, mentions=(), *, user_ids=(), owner_dm=False, owner_at=False):
        body = body.replace('\r\n', '\n').replace('\r', '\n')
        if not body.strip() or any(ord(c)<32 and c not in '\n\t' for c in body):
            raise BugFixerError('dws_before_send', 'The notification contains invalid control characters; nothing was sent.')
        args = ['chat', '+messages-send', '--as', 'bot', '--robot-code', self.config['robotCode'],
                '--title', body.splitlines()[0][:48], '--markdown', '-', '--body-stdin']
        args += ['--users', self.config['ownerStaffId']] if owner_dm else ['--groups', self.config['allowedGroupId']]
        if mentions:
            args += ['--at-open-dingtalk-ids', ','.join(mentions)]
        ids = set(user_ids) | ({self.config['ownerStaffId']} if owner_at else set())
        if ids:
            args += ['--at-user-ids', ','.join(sorted(ids))]
        # The preview must preserve the exact bytes before a service send is allowed.
        try:
            preview = self.call(args + ['--dry-run'], body)
        except BugFixerError:
            raise BugFixerError('dws_before_send', 'DWS preview failed; no send was started.') from None
        def strings(value):
            if isinstance(value, str): yield value
            elif isinstance(value, dict):
                for v in value.values(): yield from strings(v)
            elif isinstance(value, list):
                for v in value: yield from strings(v)
        if preview.get('dry_run') is not True or preview.get('executed') is not False or body not in strings(preview):
            raise BugFixerError('dws_before_send', 'DWS preview did not preserve the exact notification; nothing was sent.')
        return args, body, preview

    def send(self, body, mentions=(), *, user_ids=(), owner_dm=False, owner_at=False):
        args, body, _ = self.preview(body, mentions, user_ids=user_ids, owner_dm=owner_dm, owner_at=owner_at)
        j = self.call(args + ['--yes'], body)
        good = j.get('ok') is True and j.get('identity') in (None, 'bot')
        if owner_dm:
            r = j.get('result', {})
            details = r.get('result', {})
            good &= r.get('success') is True and not details.get('invalidStaffIdList') and not details.get('flowControlledStaffIdList')
        else:
            ledger = j.get('succeeded', [])
            good &= (j.get('succeededCount') == 1 and not j.get('failedCount') and len(ledger)==1
                     and ledger[0].get('target')==self.config['allowedGroupId']
                     and ledger[0].get('result',{}).get('success') is True)
        if not good:
            raise BugFixerError('dws_uncertain', 'DWS did not confirm the original-group delivery; do not repeat it.')
        return j

    def verify(self, body, since, *, owner_dm=False, conversation_id=None):
        target = ['--user', self.config['ownerStaffId']] if owner_dm else ['--group', self.config['allowedGroupId']]
        boundary = datetime.fromisoformat(since.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        # Shortcut projection omits sendType; raw response is needed to prove bot delivery.
        history = self.call(['chat', 'message', 'list', *target,
                             '--time', boundary, '--direction', 'newer', '--limit', '50'])
        norm = lambda x: ' '.join(x.split())
        hits = [m for m in history.get('messages', [])
                if (m.get('openConversationId') or m.get('conversationId')) == (conversation_id if owner_dm else self.config['allowedGroupId'])
                and m.get('sendType') == 'robot' and norm(m.get('content') or m.get('text', '')) == norm(body)]
        if len(hits) != 1:
            raise BugFixerError('notification_readback_pending', 'Original-group full-body readback is not yet unique; resume only reads, never resends.')
        return {'full_body_verified': True, 'message_id': hits[0].get('openMessageId') or hits[0].get('messageId'),
                'conversation_id': conversation_id if owner_dm else self.config['allowedGroupId']}
