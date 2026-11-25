import hashlib
import json
import unittest

from bugfixer.common import BugFixerError, Credential
from bugfixer.http import Response
from bugfixer.zentao import fetch, login, password_strength


def response(body):
    return Response('http://zen.wckj.com/',200,{'Content-Type':'text/html; charset=UTF-8'},body.encode())


class FakeZenTao:
    def __init__(self, auth_ok=True, md5_loaded=True):
        self.authenticated=False
        self.auth_ok=auth_ok
        self.posts=[]
        self.md5_loaded=md5_loaded

    def get(self,url):
        if url.endswith('/user-login.html'):
            definition='<script>function md5(value){return value;}</script>' if self.md5_loaded else ''
            return response("<input name='verifyRand' id='verifyRand' value='challenge-123'/>"+definition)
        if not self.authenticated:
            return response("<script>location.href='/user-login.html';</script>")
        if url.endswith('.json'):
            return response(json.dumps({'status':'success','data':json.dumps({'bug':{'id':263855,'title':'完整法语','assignedTo':'kangzijun','openedBy':'chenjing','status':'active','steps':'<p>步骤</p><img src="/file-read-437953.png">'},'actions':{'1':{'comment':'历史说明'}}})}))
        return response('<a href="/file-download-123.html">原附件</a><img src="/file-read-437953.png">')

    def post_form(self,url,values,purpose='auth'):
        self.posts.append((url,values))
        self.authenticated=self.auth_ok
        return response(json.dumps({'result':'success' if self.auth_ok else 'fail','message':'untrusted server auth detail'}))


class ZenTaoTests(unittest.TestCase):
    def test_missing_md5_uses_the_deployed_form_fallback(self):
        client=FakeZenTao(md5_loaded=False)
        login(client,Credential('fixture','fixture-password'))
        self.assertEqual(client.posts[0][1]['password'],'fixture-password')
    def test_deployed_password_strength(self):
        self.assertEqual(password_strength(''),0)
        self.assertEqual(password_strength('abc12'),0)
        self.assertEqual(password_strength('Unique12!'),1)
    def test_observed_challenge_login_and_rich_context(self):
        client=FakeZenTao()
        result=fetch(client,{'id':'263855'},Credential('fixture-user','fixture-password'))
        self.assertEqual(result['context']['assigned_to'],'kangzijun')
        self.assertEqual(result['context']['creator'],'chenjing')
        self.assertIn('步骤',result['context']['steps_text'])
        self.assertEqual(len(result['resources']),2)
        expected=hashlib.md5((hashlib.md5(b'fixture-password').hexdigest()+'challenge-123').encode()).hexdigest()
        self.assertEqual(client.posts[0][1]['password'],expected)
        self.assertNotIn('fixture-password',json.dumps(client.posts))

    def test_failed_login_is_not_retried_or_returned_as_bug(self):
        client=FakeZenTao(False)
        with self.assertRaises(BugFixerError) as error:
            fetch(client,{'id':'263855'},Credential('fixture','bad'))
        self.assertEqual(error.exception.code,'authentication_failed')
        self.assertEqual(len(client.posts),1)
        self.assertNotIn('untrusted server',error.exception.message)

    def test_missing_credentials_is_explicit(self):
        with self.assertRaises(BugFixerError) as error:
            fetch(FakeZenTao(),{'id':'263855'},Credential())
        self.assertEqual(error.exception.code,'credentials_required')


if __name__=='__main__':
    unittest.main()
