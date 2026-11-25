import argparse
import json
import sys

from .common import BugFixerError
from .credentials import connect, save_interactive
from .service import DEFAULT_DATABASE, fetch_context


def main():
    parser = argparse.ArgumentParser(prog='BugFixer')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('fetch', help='Read request JSON from protected stdin; never credentials on argv.')
    sub.add_parser('prepare-writeback', help='Prepare a repair delivery preview; JSON stdin, no remote business write.')
    sub.add_parser('apply-writeback', help='Apply an already-authorized prepared repair delivery; JSON stdin.')
    sub.add_parser('deliver', help='Inspect/run/status a scoped repair delivery pipeline; JSON stdin.')
    sub.add_parser('serve', help='Expose read-only fetch_context over MCP stdio.')
    auth = sub.add_parser('credentials', help='Manage encrypted SQLite web credentials.')
    auth.add_argument('action', choices=['init', 'save', 'list', 'edit'])
    auth.add_argument('--name')
    auth.add_argument('--source', choices=['zentao', 'customer_service'])
    auth.add_argument('--environment', choices=['production', 'test'], default='production')
    auth.add_argument('--login-url')
    args = parser.parse_args()
    try:
        if args.command == 'serve':
            from .mcp import serve
            serve()
            return
        if args.command in ('fetch', 'prepare-writeback', 'apply-writeback', 'deliver'):
            text = sys.stdin.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise BugFixerError('invalid_request', 'Request exceeds 1MiB.')
            from .writeback import prepare_writeback, apply_writeback
            from .delivery import deliver_repair
            operation = {'fetch': fetch_context, 'prepare-writeback': prepare_writeback, 'apply-writeback': apply_writeback, 'deliver': deliver_repair}[args.command]
            result = operation(json.loads(text))
        elif args.action == 'save':
            if not args.name or not args.source or not args.login_url:
                parser.error('credentials save requires --name, --source and --login-url')
            result = save_interactive(DEFAULT_DATABASE, args.name, args.source, args.environment, args.login_url)
        elif args.action == 'edit':
            from .credential_ui import run
            run(DEFAULT_DATABASE)
            return
        else:
            with connect(DEFAULT_DATABASE) as conn:
                rows = conn.execute('SELECT name,source,environment,login_url,account,protection,updated_at FROM web_credentials').fetchall()
            result = {'database': str(DEFAULT_DATABASE), 'table': 'web_credentials', 'credentials': [dict(zip(('name','source','environment','login_url','account','protection','updated_at'), row)) for row in rows]}
    except (BugFixerError, ValueError) as e:
        result = {'status': 'error', 'error': {'code': e.code if isinstance(e, BugFixerError) else 'invalid_json', 'message': e.message if isinstance(e, BugFixerError) else 'Invalid JSON request.'}}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
