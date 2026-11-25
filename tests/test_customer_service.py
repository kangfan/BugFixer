import base64
import json
import unittest
from unittest.mock import patch

from bugfixer.common import BugFixerError, Credential
from bugfixer.customer_service import fetch, _encrypt_password, PRODUCTION, SEA, SSO, TEST
from bugfixer.http import Response


def response(data=None, status=200, headers=None):
    return Response("https://example.invalid/fixture", status, headers or {}, json.dumps(data or {}).encode())


class Client:
    """Recorded-contract fixture, never an authenticated live-service claim."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {"User-Agent": "fixture"}

    def _call(self, method, url, values=None, purpose=None):
        self.calls.append((method, url, values, purpose, dict(self.headers)))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP call")
        return self.responses.pop(0)

    def get(self, url):
        return self._call("GET", url)

    def get_response(self, url, accepted_statuses=()):
        return self._call("GET_RESPONSE", url)

    def post_json(self, url, values, purpose="read"):
        return self._call("POST_JSON", url, values, purpose)


class CustomerServiceContractTests(unittest.TestCase):
    def request(self, **kwargs):
        return {"source": "customer_service", "id": "2223144", "environment": "production", "region": "domestic_minigame", **kwargs}

    def error(self, code, client, request=None, credential=None):
        with self.assertRaises(BugFixerError) as caught:
            fetch(client, request or self.request(), credential or Credential())
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(client.headers, {"User-Agent": "fixture"})
        return caught.exception

    def test_ticket_logs_media_history_and_routing(self):
        ticket = {"id": 2223144, "gameType": 2, "region": 5, "state": 2,
                  "question": "<p>签约后黑屏</p>", "appVersion": "fixture-version", "deviceType": "fixture-device", "channel": "SH5", "channelInfo": "H5_WX", "serverId": "fixture-server", "happenTime": "fixture-occurrence", "ossLogPath": "https://logs.invalid/a,https://logs.invalid/b,https://logs.invalid/a",
                  "attachments": [{"type": 0, "url": "https://media.invalid/image"}, {"type": 3, "src": "https://media.invalid/video"}, {"type": 1, "url": "https://media.invalid/file"}],
                  "operations": [{"time": "fixture-time", "action": "reply"}], "simpleContents": [{"text": "fixture reply"}]}
        client = Client([response({"username": "fixture"}), response({"errorCode": 0, "data": ticket})])
        result = fetch(client, self.request(), Credential())
        self.assertEqual([r["kind"] for r in result["resources"]], ["log", "log", "image", "video", "attachment"])
        self.assertEqual(result["context"]["description_text"], "签约后黑屏")
        self.assertEqual(result["context"]["app_version"], "fixture-version")
        self.assertEqual(result["context"]["device_type"], "fixture-device")
        self.assertEqual(result["context"]["channel"], "H5_WX")
        self.assertEqual(result["context"]["game_region"], "SH5")
        self.assertEqual(result["context"]["happened_at"], "fixture-occurrence")
        self.assertEqual(result["context"]["history"]["operations"], ticket["operations"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(client.calls[1][:4], ("POST_JSON", PRODUCTION + "/ticket/querySingle", {"id": 2223144}, "read"))
        self.assertEqual(client.calls[1][4]["REGION"], "5")
        self.assertEqual(client.calls[1][4]["IAC-GAMES"], "2")
        self.assertEqual(client.headers, {"User-Agent": "fixture"})

    def test_overseas_uses_actual_sea_origin(self):
        client = Client([response({"username": "fixture"}), response({"errorCode": 0, "data": {"id": 319220, "gameType": "2"}})])
        result = fetch(client, self.request(id="319220", region="overseas"), Credential())
        self.assertEqual(client.calls[1][1], SEA + "/ticket/querySingle")
        self.assertEqual(client.calls[1][4]["REGION"], "2")
        self.assertEqual(len(result["warnings"]), 1)

    def test_russian_region_is_not_merged_russian_region(self):
        client = Client([response(), response({"errorCode": 0, "data": {"id": "2223144", "region": 4}})])
        fetch(client, self.request(region="overseas_ru"), Credential())
        self.assertEqual(client.calls[1][4]["REGION"], "4")

    def test_test_domestic_existing_session_has_separate_origin(self):
        client = Client([response(), response({"errorCode": 0, "data": {"id": "2223144"}})])
        fetch(client, self.request(environment="test"), Credential())
        self.assertEqual(client.calls[1][1], TEST + "/ticket/querySingle")

    def test_unsupported_test_overseas_stops_before_network(self):
        client = Client([])
        self.error("unsupported_environment", client, self.request(environment="test", region="overseas"))
        self.assertEqual(client.calls, [])

    def test_invalid_id_does_not_query_player_id_or_fan_out(self):
        client = Client([])
        self.error("invalid_request", client, self.request(id="mdir2n"))
        self.assertEqual(client.calls, [])

    def test_wrong_ticket_game_or_region_never_downloaded(self):
        for ticket in [{"id": 99}, {"id": 2223144, "gameType": 1}, {"id": 2223144, "region": 2}]:
            with self.subTest(ticket=ticket):
                client = Client([response(), response({"errorCode": 0, "data": ticket})])
                self.error("identity_mismatch", client)

    def test_rejected_query_and_changed_response_do_not_expose_server_message(self):
        client = Client([response(), response({"errorCode": 401, "message": "fixture-secret"})])
        error = self.error("context_unavailable", client)
        self.assertNotIn("fixture-secret", str(error))
        self.error("contract_changed", Client([response(), Response("x", 200, {}, b"<html>login</html>")]))

    def test_missing_credential_does_not_try_password(self):
        client = Client([response(status=401)])
        self.error("credentials_required", client)
        self.assertEqual(len(client.calls), 1)

    def login_responses(self, *, switch=False, login_code=0):
        challenge = SSO + "/?systemId=gala-csc&redirect=https%3A%2F%2Fcscmanage.galasports.com%2Fhtml%2Findex.html"
        return [response(status=401), response(status=401, headers={"URL": challenge}), response(), response({"code": login_code}), response({"code": 0, "data": switch}), response(), response({"username": "fixture"}), response({"errorCode": 0, "data": {"id": 2223144}})]

    def test_password_login_encrypts_and_rechecks_selected_backend(self):
        client = Client(self.login_responses())
        fetch(client, self.request(), Credential("fixture@invalid", "fixture-password"))
        auth = next(c for c in client.calls if c[1] == SSO + "/session/login")
        self.assertEqual(auth[3], "auth")
        self.assertEqual(auth[2]["email"], "fixture@invalid")
        self.assertEqual(auth[2]["systemId"], "gala-csc")
        self.assertNotEqual(auth[2]["password"], "fixture-password")
        self.assertEqual(len(base64.b64decode(auth[2]["password"])), 256)
        self.assertEqual(client.calls[-1][1], PRODUCTION + "/ticket/querySingle")

    def test_interactive_verification_does_not_send_code_or_query_ticket(self):
        client = Client(self.login_responses(switch=True))
        self.error("needs_user_login", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertFalse(any("/verifyCode" in c[1] or "/ticket/" in c[1] for c in client.calls))

    def test_unknown_verification_shape_does_not_bypass_challenge(self):
        client = Client(self.login_responses(switch={}))
        self.error("browser_fallback_required", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertFalse(any("/ticket/" in c[1] for c in client.calls))

    def test_password_spaces_are_not_trimmed_before_frontend_rsa_step(self):
        client = Client(self.login_responses())
        with patch("bugfixer.customer_service._encrypt_password", return_value="encrypted-fixture") as encrypt:
            fetch(client, self.request(), Credential(" fixture@invalid ", " fixture "))
        encrypt.assert_called_once_with(" fixture ")

    def test_bad_password_is_not_retried(self):
        client = Client(self.login_responses(login_code=1))
        self.error("authentication_failed", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertEqual(sum(c[1].endswith("/session/login") for c in client.calls), 1)

    def test_challenge_cannot_redirect_credentials_to_other_host(self):
        client = Client([response(status=401), response(status=401, headers={"url": "https://evil.invalid/?systemId=x&redirect=https://cscmanage.galasports.com/html/index.html"})])
        self.error("browser_fallback_required", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertFalse(any(c[0] == "POST_JSON" for c in client.calls))

    def test_cookie_login_failure_is_not_reported_as_success(self):
        responses = self.login_responses()
        responses[-2] = response(status=401)
        client = Client(responses)
        self.error("browser_fallback_required", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertFalse(any("/ticket/" in c[1] for c in client.calls))

    def test_permission_denied_does_not_try_password(self):
        client = Client([response(status=403)])
        self.error("permission_denied", client, credential=Credential("fixture@invalid", "fixture"))
        self.assertEqual(len(client.calls), 1)

    def test_rsa_is_randomized_not_plaintext_or_deterministic_hash(self):
        a, b = _encrypt_password("fixture"), _encrypt_password("fixture")
        self.assertNotEqual(a, b)
        self.assertEqual(len(base64.b64decode(a)), 256)

    def test_invalid_attachment_scheme_and_unexpected_log_shape_fail_closed(self):
        for ticket in [{"id": 2223144, "ossLogPath": ["https://logs.invalid/a"]}, {"id": 2223144, "attachments": [{"type": 3, "src": "file:///sensitive"}]}]:
            client = Client([response(), response({"errorCode": 0, "data": ticket})])
            with self.assertRaises(BugFixerError):
                fetch(client, self.request(), Credential())


if __name__ == "__main__":
    unittest.main()
