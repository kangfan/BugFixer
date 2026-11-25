"""Bounded Git operations for an explicitly scoped ordinary repair delivery."""
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .common import BugFixerError


class Git:
    def __init__(self, repo, branch):
        self.repo = Path(repo).resolve()
        self.branch = branch
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,150}', branch) or '..' in branch or '//' in branch:
            raise BugFixerError('invalid_branch', 'Use an existing, explicit branch name.')
        if Path(self.call('rev-parse', '--show-toplevel').strip()).resolve() != self.repo:
            raise BugFixerError('wrong_repository', 'The path is not the repository root.')
        self.call('check-ref-format', '--branch', branch)
        self.assert_branch()

    def call(self, *args, timeout=30, allow_failure=False):
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never'}
        try:
            p = subprocess.run(['git', '--literal-pathspecs', '-C', str(self.repo), *args],
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=env,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise BugFixerError('git_timeout', 'Git timed out; reconcile the operation before any retry.') from None
        if len(p.stdout) + len(p.stderr) > 16 * 1024 * 1024:
            raise BugFixerError('git_output_limit', 'Git output exceeded the local bound.')
        if p.returncode and not allow_failure:
            # Remote URLs and stderr can contain credentials; never return them.
            raise BugFixerError('git_failed', 'Git rejected the requested operation; raw diagnostics were withheld.')
        return p.stdout.decode('utf-8') if not allow_failure else (p.returncode, p.stdout.decode('utf-8'))

    def assert_branch(self):
        if self.call('symbolic-ref', '--short', 'HEAD').strip() != self.branch:
            raise BugFixerError('branch_conflict', 'The checked-out branch changed.')

    def head(self):
        return self.call('rev-parse', 'HEAD').strip()

    def remote(self):
        rows = self.call('ls-remote', '--exit-code', 'origin', 'refs/heads/' + self.branch).splitlines()
        if len(rows) != 1:
            raise BugFixerError('remote_branch_missing', 'The expected origin branch is unavailable.')
        return rows[0].split()[0]

    def path(self, value):
        if (not isinstance(value, str) or not value or '\\' in value or ':' in value
                or any(c in value for c in '\x00\n\r') or value.startswith('/')
                or any(p in ('', '.', '..', '.git') for p in value.lower().split('/'))):
            raise BugFixerError('invalid_file', 'File paths must be literal repository-relative paths.')
        p = self.repo / value
        if not p.resolve().is_relative_to(self.repo) or p.is_symlink() or p.is_dir():
            raise BugFixerError('invalid_file', 'Directories, symlinks and paths outside the repository are unsupported.')
        for parent in p.parents:
            if parent == self.repo:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise BugFixerError('invalid_file', 'Selected files cannot traverse symlink or junction directories.')
        # Only ordinary assets and known Hotfix module locations are automated.
        allowed = (value.startswith('Assets/LoadResources/') and p.suffix.lower() in
                   ('.prefab', '.mat', '.asset', '.meta', '.png', '.jpg', '.jpeg', '.webp', '.shader', '.hlsl', '.cginc'))
        allowed |= value.startswith(('Assets/Scripts/Module/', 'Assets/Scripts/Hotfix/')) and p.suffix.lower() in ('.cs', '.meta')
        if not allowed or re.search(r'(^|/)(aot|plugins)(/|$)', value, re.I):
            raise BugFixerError('special_scope_unsupported', 'This file needs Agent review; the pipeline only automates ordinary assets/Hotfix modules.')
        return p

    def hashes(self, files):
        result = {}
        for value in files:
            p = self.path(value)
            if p.exists():
                result[value] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                code, _ = self.call('cat-file', '-e', 'HEAD:' + value, allow_failure=True)
                if code:
                    raise BugFixerError('missing_file', 'A selected file exists neither in HEAD nor in the worktree.')
                result[value] = None
        return result

    def inspect(self, files):
        self.assert_branch()
        if any(self.call('config','--get',key,allow_failure=True)[0] != 0 for key in ('user.name','user.email')):
            raise BugFixerError('git_identity_missing','Configure the actual Git author identity before delivery; no identity was invented.')
        if self.call('diff', '--cached', '--name-only', '-z') or self.call('ls-files', '-u'):
            raise BugFixerError('staged_changes', 'Existing staged or unmerged changes require review; nothing was reset.')
        self.call('diff', '--check', '--', *files)
        changed = set(self.call('diff', '--no-renames', '--name-only', '-z', 'HEAD', '--', *files).strip('\0').split('\0')) - {''}
        changed |= set(self.call('ls-files', '--others', '--exclude-standard', '-z', '--', *files).strip('\0').split('\0')) - {''}
        outside = set(self.call('diff', '--no-renames', '--name-only', '-z', 'HEAD').strip('\0').split('\0')) - set(files) - {''}
        outside |= set(self.call('ls-files', '--others', '--exclude-standard', '-z').strip('\0').split('\0')) - set(files) - {''}
        # Outside files are not submitted. Capture hashes without printing their contents.
        outside_hashes = {f: self.file_hash(self.repo/f) for f in outside}
        hashes = self.hashes(files)
        blobs = {f: self.call('hash-object', '--path=' + f, '--', f).strip() if value is not None else None for f, value in hashes.items()}
        return {'head': self.head(), 'remote': self.remote(), 'files': hashes, 'blobs': blobs,
                'changed': sorted(changed), 'outside': outside_hashes}

    @staticmethod
    def file_hash(path):
        if path.is_symlink():
            return 'link:' + os.readlink(path)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def matches_commit(self, sha, baseline, message):
        if self.call('log', '-1', '--format=%P', sha).strip() != baseline['head']:
            return False
        # Project hooks may flatten lines; every non-whitespace token must survive.
        if self.call('log', '-1', '--format=%B', sha).split() != message.split():
            return False
        changed = set(self.call('diff-tree', '--no-commit-id', '--no-renames', '--name-only', '-r', '-z', sha).strip('\0').split('\0')) - {''}
        if changed != set(baseline['changed']):
            return False
        for name in changed:
            code, blob = self.call('rev-parse', '--verify', sha + ':' + name, allow_failure=True)
            if baseline['files'][name] is None:
                if not code:
                    return False
            elif code:
                return False
            else:
                # Git clean filters/CRLF conversion legitimately differ from worktree bytes.
                if blob.strip() != baseline['blobs'][name]:
                    return False
        return all(self.file_hash(self.repo/name) == expected for name, expected in baseline['outside'].items())

    def commit(self, baseline, message):
        self.assert_branch()
        if self.inspect(list(baseline['files'])) != baseline:
            raise BugFixerError('worktree_conflict', 'Git state changed since inspection.')
        if not baseline['changed']:
            raise BugFixerError('no_changes', 'No selected changes are ready for delivery.')
        self.call('add', '--', *baseline['changed'])
        # --only prevents accidentally including somebody else's concurrent staging.
        self.call('commit', '--only', '-m', message, '--', *baseline['changed'], timeout=120)
        sha = self.head()
        if not self.matches_commit(sha, baseline, message):
            raise BugFixerError('commit_mismatch', 'The created commit differs from the inspected repair; it was not pushed.')
        for name, expected in baseline['outside'].items():
            p = self.repo/name
            actual = self.file_hash(p)
            if actual != expected:
                raise BugFixerError('outside_change', 'An unrelated worktree file changed; the commit was not pushed.')
        return sha

    def pushed(self, sha):
        tip = self.remote()
        if tip == sha:
            return True
        code, _ = self.call('merge-base', '--is-ancestor', sha, tip, allow_failure=True)
        return code == 0

    def push(self, sha, expected_remote):
        self.assert_branch()
        if self.remote() != expected_remote:
            raise BugFixerError('remote_conflict', 'The origin branch changed; no force push, merge or rebase was attempted.')
        self.call('push', 'origin', sha + ':refs/heads/' + self.branch, timeout=120)
        if not self.pushed(sha):
            raise BugFixerError('push_uncertain', 'The remote does not yet prove the expected commit.')
