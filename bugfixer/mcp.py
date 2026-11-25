"""Small dependency-free MCP stdio server (JSON-RPC, newline framing)."""
import json
import sys

from . import __version__
from .service import TOOL_SCHEMA, fetch_context
from .writeback import PREPARE_SCHEMA, APPLY_SCHEMA, prepare_writeback, apply_writeback
from .delivery import DELIVERY_SCHEMA, deliver_repair


def handle(message, fetch=fetch_context):
    if not isinstance(message,dict):
        return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request.'}}
    ident = message.get('id')
    if 'id' not in message:
        return None
    method = message.get('method')
    params = message.get('params') or {}
    if not isinstance(params,dict):
        return {'jsonrpc':'2.0','id':ident,'error':{'code':-32602,'message':'params must be an object.'}}
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'BugFixer', 'version': __version__}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL_SCHEMA, PREPARE_SCHEMA, APPLY_SCHEMA, DELIVERY_SCHEMA]}
    elif method == 'tools/call':
        operation = {'fetch_context': fetch, 'prepare_writeback': prepare_writeback, 'apply_writeback': apply_writeback, 'deliver_repair': deliver_repair}.get(params.get('name'))
        if operation is None:
            return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
        data = operation(params.get('arguments') or {})
        result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}], 'isError': data.get('status') in ('blocked', 'error', 'uncertain', 'conflict', 'needs_approval', 'needs_agent')}
    else:
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found.'}}
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}


def serve():
    for line in sys.stdin:
        try:
            if len(line) > 1024 * 1024:
                raise ValueError()
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError()
            result = handle(message)
        except (ValueError, TypeError):
            result = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON-RPC message.'}}
        except Exception:
            # Never serialize raw auth/HTTP exceptions, which may contain credentials.
            result = {'jsonrpc': '2.0', 'id': message.get('id'), 'error': {'code': -32603, 'message': 'Internal tool error; no raw credential-bearing exception was emitted.'}}
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
