"""Sign in with Google on the admin dashboard: only a listed, verified address."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from admin import app as admin_app
from admin import google_login
from config import config as real_config

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import set_admin_google  # noqa: E402

EMAIL = "owner@example.com"
ON = dataclasses.replace(
    real_config,
    dashboard_user="admin",
    dashboard_password="pw",
    admin_google_client_id="cid.apps.googleusercontent.com",
    admin_google_client_secret="SECRET",
    admin_google_emails=f"{EMAIL}, Other@Example.com",
    admin_public_url="https://box.example.ts.net/",
)
OFF = dataclasses.replace(ON, admin_google_client_secret="")


def claims(**over) -> dict:
    return {
        "iss": "https://accounts.google.com",
        "aud": ON.admin_google_client_id,
        "exp": 2_000,
        "nonce": "n",
        "email": EMAIL,
        "email_verified": True,
    } | over


class ClaimTests(unittest.TestCase):
    def _check(self, **over):
        return google_login.verified_email(claims(**over), ON, "n", now=1_000)

    def test_listed_verified_address_passes(self) -> None:
        self.assertEqual(self._check(), EMAIL)
        self.assertEqual(self._check(email="OTHER@example.com"), "other@example.com")

    def test_everything_else_is_refused(self) -> None:
        for over in (
            {"email": "stranger@example.com"},
            {"email_verified": False},
            {"email_verified": "true"},
            {"email": ""},
            {"aud": "another-app"},
            {"iss": "https://evil.example"},
            {"exp": 999},
            {"exp": None},
            {"nonce": "other"},
        ):
            with self.subTest(over), self.assertRaises(google_login.LoginError):
                self._check(**over)

    def test_empty_nonce_never_matches(self) -> None:
        with self.assertRaises(google_login.LoginError):
            google_login.verified_email(claims(nonce=""), ON, "", now=1_000)

    def test_off_unless_fully_configured(self) -> None:
        self.assertTrue(google_login.is_enabled(ON))
        self.assertFalse(google_login.is_enabled(OFF))
        self.assertFalse(
            google_login.is_enabled(dataclasses.replace(ON, admin_google_emails=" , "))
        )
        self.assertFalse(google_login.is_enabled(dataclasses.replace(ON, admin_public_url="")))

    def test_unreadable_token(self) -> None:
        for bad in ("", "abc", "a.!!!.c"):
            with self.subTest(bad), self.assertRaises(google_login.LoginError):
                google_login._claims(bad)
        raw = base64.urlsafe_b64encode(json.dumps(claims()).encode()).rstrip(b"=").decode()
        self.assertEqual(google_login._claims(f"h.{raw}.s")["email"], EMAIL)


class RouteTests(unittest.TestCase):
    def _client(self, cfg=ON):
        patcher = mock.patch.object(admin_app, "pipeline_config", cfg)
        patcher.start()
        self.addCleanup(patcher.stop)
        return admin_app.create_app().test_client()

    def _start_login(self, client) -> dict:
        resp = client.get("/login/google")
        self.assertEqual(resp.status_code, 302)
        return urllib.parse.parse_qs(urllib.parse.urlparse(resp.location).query)

    def _callback(self, client, query, payload):
        with mock.patch.object(google_login, "exchange_code", return_value=payload):
            return client.get(
                google_login.CALLBACK_PATH, query_string={"state": query["state"][0], "code": "c"}
            )

    def test_redirect_carries_pkce_and_our_callback(self) -> None:
        query = self._start_login(self._client())
        self.assertEqual(
            query["redirect_uri"], ["https://box.example.ts.net/auth/google/callback"]
        )
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], ["openid email"])

    def test_signed_out_goes_to_login_page_not_a_password_prompt(self) -> None:
        resp = self._client().get("/config")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith("/login"))

    def test_google_off_keeps_the_old_password_prompt(self) -> None:
        resp = self._client(OFF).get("/config")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Basic", resp.headers["WWW-Authenticate"])

    def test_password_still_works_with_google_on(self) -> None:
        client = self._client()
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
        self.assertEqual(client.get("/login/password").status_code, 401)
        self.assertEqual(client.get("/login/password", headers=auth).status_code, 302)
        with mock.patch.object(admin_app.env_schema, "parse", return_value=[]):
            self.assertEqual(client.get("/config", headers=auth).status_code, 200)
        wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:no").decode()}
        self.assertEqual(client.get("/config", headers=wrong).status_code, 302)

    def test_full_login_then_logout_needs_csrf(self) -> None:
        client = self._client()
        query = self._start_login(client)
        payload = claims(nonce=query["nonce"][0], exp=9_999_999_999)
        self.assertTrue(self._callback(client, query, payload).location.endswith("/"))
        with mock.patch.object(admin_app.env_schema, "parse", return_value=[]):
            self.assertEqual(client.get("/config").status_code, 200)
        self.assertEqual(client.post("/logout").status_code, 400)  # no CSRF token

    def test_stranger_gets_no_session(self) -> None:
        client = self._client()
        query = self._start_login(client)
        payload = claims(nonce=query["nonce"][0], exp=9_999_999_999, email="x@example.com")
        self.assertTrue(self._callback(client, query, payload).location.endswith("/login"))
        self.assertEqual(client.get("/config").status_code, 302)

    def test_forged_state_never_reaches_google(self) -> None:
        client = self._client()
        self._start_login(client)
        with mock.patch.object(google_login, "exchange_code") as exchange:
            resp = client.get(
                google_login.CALLBACK_PATH, query_string={"state": "forged", "code": "c"}
            )
        exchange.assert_not_called()
        self.assertTrue(resp.location.endswith("/login"))

    def test_callback_without_starting_a_login(self) -> None:
        with mock.patch.object(google_login, "exchange_code") as exchange:
            resp = self._client().get(google_login.CALLBACK_PATH, query_string={"code": "c"})
        exchange.assert_not_called()
        self.assertTrue(resp.location.endswith("/login"))


class SetupScriptTests(unittest.TestCase):
    GOOD_ID = "abc.apps.googleusercontent.com"

    def test_client_check(self) -> None:
        check = set_admin_google.check_client
        self.assertIsNone(check(self.GOOD_ID, "s", lambda *_: "invalid_grant"))
        self.assertIn("recognise", check(self.GOOD_ID, "s", lambda *_: "invalid_client"))
        self.assertIn("client ID", check("nope", "s", lambda *_: "invalid_grant"))
        self.assertIn("empty", check(self.GOOD_ID, "", lambda *_: "invalid_grant"))
        self.assertIn("confirm", check(self.GOOD_ID, "s", lambda *_: "network: URLError"))

    def _run(self, env: Path, answers, error):
        it = iter(answers)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = set_admin_google.run(
                env, restart=False, probe=lambda *_: error, ask=lambda _p: next(it)
            )
        return code, out.getvalue()

    def test_saves_a_good_pair_and_never_prints_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_bytes(b"ADMIN_PUBLIC_URL=https://x\nADMIN_GOOGLE_EMAILS=a@b.c\n")
            code, out = self._run(env, [self.GOOD_ID, "S3CRET"], "invalid_grant")
            self.assertEqual(code, 0)
            self.assertIn(b"ADMIN_GOOGLE_CLIENT_SECRET=S3CRET", env.read_bytes())
            self.assertNotIn("S3CRET", out)
            self.assertNotIn(self.GOOD_ID, out)

    def test_bad_pair_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_bytes(b"X=1\n")
            code, out = self._run(env, [self.GOOD_ID, "S3CRET"], "invalid_client")
            self.assertEqual(code, 1)
            self.assertEqual(env.read_bytes(), b"X=1\n")
            self.assertNotIn("S3CRET", out)


if __name__ == "__main__":
    unittest.main()
