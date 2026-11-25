import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bugfixer.artifacts import ArtifactStore, extract_zip
from bugfixer.common import BugFixerError, public_url, redact
from bugfixer.http import DOWNLOAD_HOSTS, validate_url, validate_download_url
from bugfixer.mcp import handle
from bugfixer.service import fetch_context, validate_request


class Stream(io.BytesIO):
    def __init__(self, body, content_type='application/octet-stream'):
        super().__init__(body)
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}


class FakeDownloadClient:
    hosts = DOWNLOAD_HOSTS

    def __init__(self, data):
        self.data = data
        self.calls = []

    def open(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith('/missing'):
            raise BugFixerError('network_error', 'fixture timeout', True)
        return Stream(self.data)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_task_urls_and_write_operations(self):
        for request in ({'source':'zentao','id':'../12'}, {'source':'zentao','id':'12','operation':'resolve'}, {'source':'customer_service','id':'12'}, {'source':'zentao','id':'12','base_url':'http://evil/'}):
            with self.assertRaises(BugFixerError):
                validate_request(request)

    def test_url_allowlist_covers_redirect_and_credential_exfiltration(self):
        for url in ('file:///etc/passwd','http://127.0.0.1/','http://zen.wckj.com.evil/','http://user:pass@zen.wckj.com/','https://cscmanage.galasports.com:8000/'):
            with self.assertRaises(BugFixerError):
                validate_url(url)
        validate_url('https://cscmanage-sea.galasports.com:8000/ticket/querySingle')

    def test_attachment_cannot_invoke_business_action_on_authenticated_service(self):
        for url in ('http://zen.wckj.com/bug-delete-263855.html?x=/file-read-1.png','https://cscmanage.galasports.com/ticket/delete?id=1','http://zen.wckj.com/data/upload/../bug-delete-1.html'):
            with self.assertRaises(BugFixerError):
                validate_download_url(url)
        validate_download_url('http://zen.wckj.com/file-read-437953.png')
        validate_download_url('https://galasports-img.oss-cn-beijing.aliyuncs.com/GALA-CSC/2/hash')

    def test_secret_redaction_and_signed_url(self):
        result = redact({'password':'secret','comment':'password=secret and exact unique-value','nested':[{'accessToken':'abc'}]}, ('unique-value',))
        text = json.dumps(result)
        self.assertNotIn('secret', text)
        self.assertNotIn('unique-value', text)
        self.assertNotIn('abc', text)
        self.assertNotIn('signature123', public_url('https://galasports-img.oss-cn-beijing.aliyuncs.com/log?Signature=signature123'))

    def test_plain_text_is_saved_and_indexed_despite_zip_suffix(self):
        run = self.root/'run'; run.mkdir()
        store = ArtifactStore(self.root)
        result = store.download(FakeDownloadClient(b'2026-11-24 ERROR timeout\nhello'), {'url':'http://zen.wckj.com/log.zip','kind':'log'},run,0)
        self.assertEqual(result['detected_type'],'text')
        self.assertEqual(result['index']['line_count'],2)
        self.assertTrue(Path(result['path']).exists())

    def test_zip_paths_are_validated_before_extracting_any_member(self):
        archive = self.root/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('safe.txt','first')
            z.writestr('../escape.txt','bad')
        with self.assertRaises(BugFixerError):
            extract_zip(archive,self.root/'out')
        self.assertFalse((self.root/'out'/'safe.txt').exists())
        self.assertFalse((self.root/'escape.txt').exists())

    def test_partial_download_failure_preserves_successful_context(self):
        def adapter(client,request,credential):
            return {'context':{'id':'12','title':'context survives','steps':'password=fixture-secret'},'resources':[{'url':'http://zen.wckj.com/good','name':'first.log'},{'url':'http://zen.wckj.com/missing','name':'second.log'}]}
        result=fetch_context({'source':'zentao','id':'12','credentials':{'account':'x','password':'fixture-secret'}},output_root=self.root,client=FakeDownloadClient(b'Exception: test'),adapter=adapter)
        self.assertEqual(result['status'],'partial')
        self.assertEqual([r['status'] for r in result['resources']],['downloaded','failed'])
        self.assertTrue(Path(result['context_path']).exists())
        self.assertNotIn('fixture-secret',Path(result['context_path']).read_text())
        self.assertFalse(result['handoff']['agent_spawned_by_tool'])

    def test_context_is_fresh_and_cache_reuse_is_hash_verified(self):
        def adapter(client,request,credential):
            return {'context':{'id':'12','status':'active'},'resources':[{'url':'http://zen.wckj.com/log'}]}
        client=FakeDownloadClient(b'line\nerror')
        one=fetch_context({'source':'zentao','id':'12'},output_root=self.root,client=client,adapter=adapter)
        two=fetch_context({'source':'zentao','id':'12'},output_root=self.root,client=client,adapter=adapter)
        self.assertNotEqual(one['context_path'],two['context_path'])
        self.assertTrue(two['resources'][0]['cache_reused'])
        self.assertEqual(len(client.calls),2) # Still refresh remote bytes; no stale mutable URL assumption.

    def test_html_is_not_treated_as_downloaded_log(self):
        run=self.root/'run';run.mkdir()
        with self.assertRaises(BugFixerError) as error:
            ArtifactStore(self.root).download(FakeDownloadClient(b'<html>login</html>'),{'url':'http://zen.wckj.com/log'},run,0)
        self.assertEqual(error.exception.code,'unexpected_html')

    def test_mcp_exposes_explicit_read_prepare_apply_and_hands_back_to_caller(self):
        tools=handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']['tools']
        self.assertEqual([t['name'] for t in tools],['fetch_context','prepare_writeback','apply_writeback','deliver_repair'])
        self.assertTrue(tools[0]['annotations']['readOnlyHint'])
        self.assertTrue(tools[1]['annotations']['readOnlyHint'])
        self.assertFalse(tools[2]['annotations']['readOnlyHint'])
        called=[]
        result=handle({'id':2,'method':'tools/call','params':{'name':'fetch_context','arguments':{'id':'12'}}},lambda r: called.append(r) or {'status':'ok'})
        self.assertEqual(called,[{'id':'12'}])
        self.assertFalse(result['result']['isError'])
        self.assertIsNone(handle({'method':'notifications/initialized'}))


if __name__=='__main__':
    unittest.main()
