"""Memory never keeps a secret or a one-time code, from any writer; where a secret is
kept (a vault item's name, an env var, a file) is kept."""
import tempfile
import unittest
from pathlib import Path

from deltos.store import Store, secret_problem


class SecretTest(unittest.TestCase):
    def test_refused(self):
        for label, value in (("Text code", "153232"), ("Verification code", "153 232"), ("PIN", "4821"),
                             ("Password", "mem0graph"), ("api key", "sk-abcdefghijklmnop1234"),
                             ("Notes", "uses ghp_abcdefghijklmnopqrstuvwx1234 for pushes"),
                             ("OTP", "889911")):
            self.assertTrue(secret_problem(value, label), (label, value))

    def test_kept(self):
        for label, value in (("Zip code", "78701"), ("Exit code", "137"), ("Status code", "200"),
                             ("Password", "in the vault"), ("Token", "none"), ("Setting hermes.apiKey", "configured via local.properties"),
                             ("Setting password source", "~/workspace/mailer/.env"), ("Api key", "BRAIN_API_KEY"),
                             ("Pin", ":8000"), ("PAT vault item", "Netlify - Acme PAT"), ("Port", "153232"),
                             ("Commit", "07cddff"), ("Code", "a Kotlin app")):
            self.assertEqual(secret_problem(value, label), "", (label, value))

    def test_every_writer_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d), today="2026-05-01")
            s.name("Registrar", [], "account")
            for kw in ({}, {"confirm": False}, {"source": "hand"}):
                r = s.remember("Registrar", "Text code", "153232", **kw)
                self.assertTrue(r.startswith("not recorded: it looks like a one-time code"), r)
            self.assertNotIn("153232", (Path(d) / "notes" / "registrar.md").read_text())


if __name__ == "__main__":
    unittest.main()
