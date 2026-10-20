#!/usr/bin/env python3
"""Tests for zammad-cato-meldingen.py — run with: /usr/bin/python3 bin/test_zammad_cato_meldingen.py

No network, no Zammad, no CATO: Zammad and the cato-CLI are fakes below.
ZCM_PATH=<copy> points the suite at a mutated copy (break-the-test probe)."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone

MODULE_PATH = os.environ.get(
    "ZCM_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "zammad-cato-meldingen.py"))
spec = importlib.util.spec_from_file_location("zcm", MODULE_PATH)
zcm = importlib.util.module_from_spec(spec)
sys.modules["zcm"] = zcm
spec.loader.exec_module(zcm)

SINDS = datetime(2026, 10, 1, tzinfo=timezone.utc)


def ticket(tid, nummer, email, created="2026-10-10T22:30:00Z", state="open"):
    return {"id": tid, "number": str(nummer), "customer_id": 1000 + tid,
            "created_at": created, "state": state, "_email": email}


class FakeZammad:
    def __init__(self, tickets, tags=None, alle=None):
        self.tickets, self._tags = tickets, tags or {}
        self.alle = alle if alle is not None else tickets

    def escalaties(self, sinds):
        return [t for t in self.tickets
                if datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) >= sinds]

    def email(self, uid):
        return next(t["_email"] for t in self.tickets + self.alle if t["customer_id"] == uid)

    def tags(self, tid):
        return self._tags.get(tid, [])

    def ticket_op_nummer(self, nummer):
        return next((t for t in self.alle if t["number"] == str(nummer)), None)


class FakeCato:
    """Minimal model of the four cato calls the script makes."""

    def __init__(self, companies, notes=None):
        self.companies, self.notes, self.calls = companies, list(notes or []), []

    def __call__(self, *args):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["companies", "list"]:
            d = a[a.index("--domain") + 1]
            return [c for c in self.companies
                    if d in (c["domainName"].get("primaryLinkUrl") or "")]
        if a[:2] == ["notes", "list"]:
            t = a[a.index("--title") + 1]
            rows = [n for n in self.notes if t in n["title"]]
            if "--open" in a:
                rows = [n for n in rows if n.get("afhandeling") == "OPEN"]
            return rows
        if a[:2] == ["notes", "create"]:
            self.notes.append({"id": f"new{len(self.notes)}", "title": a[a.index("--title") + 1],
                               "afhandeling": "OPEN", "company": a[a.index("--company-id") + 1]})
            return {}
        if a[:2] == ["notes", "update"]:
            n = next(n for n in self.notes if n["id"] == a[2])
            n["afhandeling"] = a[a.index("--afhandeling") + 1]
            return {}
        raise AssertionError(f"onverwachte cato-aanroep {args}")

    def writes(self):
        return [c for c in self.calls if c[1] in ("create", "update")]


def bedrijf(cid, url):
    return {"id": cid, "domainName": {"primaryLinkUrl": url, "secondaryLinks": []}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.companies = [bedrijf("c-kantoor", "https://www.kantoor.nl"),
                          bedrijf("c-grab", "https://grabkantoor.nl"),
                          bedrijf("c-dup1", "https://dubbel.nl"),
                          bedrijf("c-dup2", "http://dubbel.nl/")]

    def run_ronde(self, tickets, notes=None, schrijven=True, tags=None, alle=None):
        cato = FakeCato(self.companies, notes)
        tel = zcm.ronde(FakeZammad(tickets, tags, alle), cato, SINDS, schrijven)
        return tel, cato

    def test_bekend_bedrijf_krijgt_klantmelding_open(self):
        tel, cato = self.run_ronde([ticket(1, 59100, "a@kantoor.nl")],
                                   tags={1: ["ai-escalate", "topic-billing", "escalation-billing"]})
        self.assertEqual(tel["aangemaakt"], 1)
        create = next(c for c in cato.calls if c[:2] == ("notes", "create"))
        self.assertIn("KLANTMELDING", create)
        self.assertEqual(create[create.index("--afhandeling") + 1], "OPEN")
        self.assertEqual(create[create.index("--company-id") + 1], "c-kantoor")
        self.assertIn("--no-dry-run", create)

    def test_titel_draagt_amsterdamse_datum_en_ticketnummer(self):
        titel, _ = zcm.notitie(ticket(1, 59100, "x"), [])
        # 22:30Z op 10-10 is 00:30 op 11-10 in Amsterdam
        self.assertEqual(titel, "Klantmelding 11-10-2026 — Zammad #59100 geëscaleerd")

    def test_body_bevat_alleen_metadata(self):
        t = dict(ticket(1, 59100, "a@kantoor.nl"), title="Geheim dossieronderwerp")
        _, body = zcm.notitie(t, ["topic-bug", "escalation-security", "escalation-email"])
        self.assertNotIn("Geheim", body)
        self.assertNotIn("kantoor", body)
        self.assertIn("Categorie: bug", body)
        self.assertIn("Signalen: security", body)
        self.assertIn(zcm.HANDTEKENING, body)

    def test_substring_domein_is_geen_treffer(self):
        # 'kantoor.nl' vindt via --domain ook 'grabkantoor.nl'; exact blijft er één over
        bed, reden = zcm.exact_bedrijf(self.companies[:2], "kantoor.nl")
        self.assertEqual((bed["id"], reden), ("c-kantoor", None))

    def test_onbekend_gratis_eigen_en_dubbel_worden_overgeslagen(self):
        tel, cato = self.run_ronde([ticket(1, 1, "a@onbekend.nl"), ticket(2, 2, "b@gmail.com"),
                                    ticket(3, 3, "c@prudai.com"), ticket(4, 4, "d@dubbel.nl"),
                                    ticket(5, 5, "")])
        self.assertEqual(tel["overgeslagen_geen_bedrijf"], 1)
        self.assertEqual(tel["overgeslagen_gratis_mail"], 1)
        self.assertEqual(tel["overgeslagen_prudai_zelf"], 1)
        self.assertEqual(tel["overgeslagen_meerdere_bedrijven"], 1)
        self.assertEqual(tel["overgeslagen_geen_email"], 1)
        self.assertEqual(cato.writes(), [])

    def test_al_genoteerd_ook_handmatig_en_niet_bij_prefix(self):
        notes = [{"id": "h1", "title": "Klantmelding 15-09-2026 — storing (#59027)",
                  "afhandeling": "OPEN"},
                 {"id": "h2", "title": "Klantmelding — Zammad #59010", "afhandeling": "OPEN"}]
        tel, cato = self.run_ronde([ticket(1, 59027, "a@kantoor.nl"),
                                    ticket(2, 5901, "a@kantoor.nl")], notes=notes)
        self.assertEqual(tel["al_genoteerd"], 1)     # #59027 bestond al
        self.assertEqual(tel["aangemaakt"], 1)       # #5901 is niet #59010

    def test_tweede_ronde_maakt_niets_dubbel(self):
        tickets = [ticket(1, 59100, "a@kantoor.nl")]
        cato = FakeCato(self.companies)
        zcm.ronde(FakeZammad(tickets), cato, SINDS, True)
        tel = zcm.ronde(FakeZammad(tickets), cato, SINDS, True)
        self.assertEqual(tel["al_genoteerd"], 1)
        self.assertEqual(sum(1 for c in cato.calls if c[:2] == ("notes", "create")), 1)

    def test_droogloop_schrijft_niets(self):
        dicht = ticket(9, 59000, "a@kantoor.nl", created="2026-09-01T10:00:00Z", state="closed")
        notes = [{"id": "e1", "title": "Klantmelding 01-09-2026 — Zammad #59000 geëscaleerd",
                  "afhandeling": "OPEN", "bodyV2": {"markdown": zcm.HANDTEKENING}}]
        tel, cato = self.run_ronde([ticket(1, 59100, "a@kantoor.nl")], notes=notes,
                                   schrijven=False, alle=[dicht])
        self.assertEqual((tel["zou_aanmaken"], tel["zou_oplossen"]), (1, 1))
        self.assertEqual(cato.writes(), [])

    def test_ouder_dan_since_telt_niet(self):
        tel, _ = self.run_ronde([ticket(1, 1, "a@kantoor.nl", created="2026-09-01T10:00:00Z")])
        self.assertEqual(tel["escalaties"], 0)

    def test_gesloten_ticket_zet_eigen_notitie_op_opgelost(self):
        dicht = ticket(9, 59000, "a@kantoor.nl", created="2026-09-01T10:00:00Z", state="closed")
        nog = ticket(8, 59001, "a@kantoor.nl", created="2026-09-01T10:00:00Z", state="open")
        notes = [
            {"id": "e1", "title": "Klantmelding 01-09-2026 — Zammad #59000 geëscaleerd",
             "afhandeling": "OPEN", "bodyV2": {"markdown": "x\n_" + zcm.HANDTEKENING + "_"}},
            {"id": "e2", "title": "Klantmelding 01-09-2026 — Zammad #59001 geëscaleerd",
             "afhandeling": "OPEN", "bodyV2": {"markdown": zcm.HANDTEKENING}},
            # handwerk (geen handtekening) over een dicht ticket: niet aankomen
            {"id": "m1", "title": "Klantmelding — Zammad #59000 nagebeld", "afhandeling": "OPEN",
             "bodyV2": {"markdown": "door Beau"}},
        ]
        tel, cato = self.run_ronde([], notes=notes, alle=[dicht, nog])
        self.assertEqual(tel["opgelost"], 1)
        updates = [c for c in cato.calls if c[:2] == ("notes", "update")]
        self.assertEqual([c[2] for c in updates], ["e1"])

    def test_helpers(self):
        self.assertEqual(zcm.host("https://WWW.Kantoor.nl/pad?x"), "kantoor.nl")
        self.assertEqual(zcm.host("kantoor.nl"), "kantoor.nl")
        self.assertEqual(zcm.mail_domein("A@Kantoor.NL"), "kantoor.nl")
        self.assertEqual(zcm.overslaan_reden("mail.prudai.com"), "prudai_zelf")
        self.assertEqual(zcm.ticketnummer_uit("Klantmelding — Zammad #59027 x"), "59027")
        self.assertIsNone(zcm.ticketnummer_uit("geen nummer"))

    def test_halve_vlaggen_weigeren(self):
        self.assertEqual(zcm.main(["--no-dry-run"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
