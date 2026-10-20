#!/usr/bin/env python3
"""Zammad-escalatie → KLANTMELDING-notitie in CATO (kaart 70bbb1f0, punt 2).

    /usr/bin/python3 bin/zammad-cato-meldingen.py                 # droogloop: zegt wat hij zou doen
    /usr/bin/python3 bin/zammad-cato-meldingen.py --no-dry-run --yes
    /usr/bin/python3 bin/zammad-cato-meldingen.py --since 2026-09-01   # verder terugkijken

Wat het doet:
1. Zoekt in Zammad de tickets met tag `ai-escalate` (gezet door zammad-autoreply
   als de AI-triage een ticket naar een medewerker escaleert), aangemaakt sinds
   `--since` (standaard 14 dagen terug).
2. Koppelt elk ticket aan een CATO-bedrijf via het e-maildomein van de klant: alleen
   bij precies één bedrijf met exact dat domein is het een "bekende klantorganisatie".
   Gratis mail, Prudai zelf, geen of meerdere treffers → overslaan (geteld, niet
   geraden). Zammad-organisaties helpen hier niet: op 20-10-2026 had 1 van de 16
   geëscaleerde tickets er een.
3. Bestaat er voor dat bedrijf nog geen notitie met `#<ticketnummer>` in de titel,
   dan maakt hij er een aan via de cato-CLI: soort KLANTMELDING, afhandeling OPEN.
4. Notities die dít script eerder maakte en nog OPEN staan, zet hij op OPGELOST zodra
   het ticket in Zammad gesloten of samengevoegd is — anders blijft de groom elke
   week vragen naar meldingen die allang klaar zijn.

Waarom een host-script en niet in zammad-autoreply zelf: het enige schrijfpad naar
CATO is de cato-CLI (AGENTS.md, "nooit rauwe REST-writes"), en die draait op de host,
niet in de container. Bijkomend: de webhook blijft precies zoals hij is, en uitrollen
vraagt geen herstart van een productiedienst — alleen een systemd-timer.

Vertrouwelijkheid: de notitie bevat alleen metadata — ticketnummer + link, datum,
categorie- en signaaltags. Geen onderwerp, geen berichttekst, geen namen; de inhoud
blijft in Zammad. De uitvoer van dit script telt alleen (geen domeinen of namen).

Leest Zammad met het bestaande autoreply-token (ticket.agent; tickets zoeken,
gebruikers en tags lezen). Schrijft niets in Zammad."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

CATO = os.environ.get("CATO_BIN", "/root/bin/cato")
ZAMMAD_BASE = os.environ.get("ZAMMAD_LOCAL_API", "http://127.0.0.1:18080/api/v1")
ZAMMAD_PUBLIC = os.environ.get("ZAMMAD_PUBLIC_BASE_URL", "https://support.prudai.com")
TOKEN_FILE = os.environ.get("ZAMMAD_AUTOREPLY_TOKEN_FILE", "/root/zammad/secrets/autoreply.token")
ESCALATIE_TAG = "ai-escalate"
TERUGKIJKEN = timedelta(days=14)
TZ = ZoneInfo("Europe/Amsterdam")
GESLOTEN_STATES = {"closed", "merged"}
# Herkenning van "door dit script gemaakt" — staat zichtbaar onderaan de notitie.
HANDTEKENING = "Automatisch vastgelegd door zammad-cato-meldingen."
GRATIS_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.nl", "outlook.com", "outlook.nl",
    "live.com", "live.nl", "msn.com", "icloud.com", "me.com", "yahoo.com", "yahoo.nl",
    "ziggo.nl", "kpnmail.nl", "kpnplanet.nl", "planet.nl", "xs4all.nl", "home.nl",
    "hetnet.nl", "casema.nl", "chello.nl", "upcmail.nl", "telfort.nl", "proton.me",
    "protonmail.com",
}
EIGEN_DOMEINEN = ("prudai.com", "prudai.nl")


# ---------- kleine helpers (zuiver, getest) ----------

def host(url: str | None) -> str:
    """'https://www.Example.nl/pad' → 'example.nl'."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    if "://" not in u:
        u = "//" + u
    h = urllib.parse.urlsplit(u).hostname or ""
    return h[4:] if h.startswith("www.") else h


def mail_domein(email: str | None) -> str:
    e = (email or "").strip().lower()
    return e.rsplit("@", 1)[1] if "@" in e else ""


def overslaan_reden(domein: str) -> str | None:
    if not domein:
        return "geen_email"
    if domein in GRATIS_MAIL:
        return "gratis_mail"
    if any(domein == d or domein.endswith("." + d) for d in EIGEN_DOMEINEN):
        return "prudai_zelf"
    return None


def bedrijf_domeinen(company: dict[str, Any]) -> set[str]:
    dn = company.get("domainName") or {}
    urls = [dn.get("primaryLinkUrl")]
    urls += [x.get("url") for x in (dn.get("secondaryLinks") or []) if isinstance(x, dict)]
    return {h for h in (host(u) for u in urls) if h}


def exact_bedrijf(kandidaten: list[dict[str, Any]], domein: str) -> tuple[dict | None, str | None]:
    """`cato companies list --domain` zoekt op substring ('ab.nl' vindt ook 'grab.nl');
    hier telt alleen een exacte host. Eén treffer = bekend; anders een reden."""
    exact = [c for c in kandidaten if domein in bedrijf_domeinen(c)]
    if len(exact) == 1:
        return exact[0], None
    return None, ("geen_bedrijf" if not exact else "meerdere_bedrijven")


def nl_datum(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%d-%m-%Y")


def notitie(ticket: dict[str, Any], tags: list[str]) -> tuple[str, str]:
    """Titel + body, alleen metadata. De datum vooraan in de titel is wat
    /cato-agenda en /cato-groom als gebeurtenisdatum lezen."""
    nummer, datum = ticket["number"], nl_datum(ticket["created_at"])
    categorie = next((t[len("topic-"):] for t in sorted(tags) if t.startswith("topic-")), None)
    signalen = sorted(t[len("escalation-"):] for t in tags
                      if t.startswith("escalation-") and t != "escalation-email")
    titel = f"Klantmelding {datum} — Zammad #{nummer} geëscaleerd"
    body = "\n".join([
        f"Zammad-ticket [#{nummer}]({ZAMMAD_PUBLIC}/#ticket/zoom/{ticket['id']}) is door de "
        f"AI-triage geëscaleerd naar een medewerker (ticket aangemaakt {datum}).",
        "",
        f"- Categorie: {categorie or 'onbekend'}",
        f"- Signalen: {', '.join(signalen) if signalen else 'geen'}",
        "",
        "De inhoud staat in Zammad en hoort niet in CATO. Afhandeling springt vanzelf op "
        "OPGELOST zodra het ticket in Zammad gesloten is.",
        "",
        f"_{HANDTEKENING}_",
    ])
    return titel, body


def ticketnummer_uit(titel: str) -> str | None:
    m = re.search(r"Zammad #(\d+)", titel or "")
    return m.group(1) if m else None


def noemt_ticket(titel: str, nummer: Any) -> bool:
    """`--title` zoekt op substring: '#5902' vindt ook '#59027'. Hier exact."""
    return re.search(rf"#{nummer}(?!\d)", titel or "") is not None


# ---------- I/O ----------

class Zammad:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def get(self, path: str, **params: Any) -> Any:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.base}{path}{'?' + qs if qs else ''}",
                                     headers={"Authorization": f"Token token={self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def escalaties(self, sinds: datetime) -> list[dict[str, Any]]:
        uit, pagina = [], 1
        while True:
            rij = self.get("/tickets/search", query=f"tags:{ESCALATIE_TAG}", limit=100,
                           page=pagina, expand="true", sort_by="created_at", order_by="desc")
            rij = rij if isinstance(rij, list) else []
            uit += rij
            if len(rij) < 100:
                break
            pagina += 1
        return [t for t in uit
                if datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) >= sinds]

    def ticket_op_nummer(self, nummer: str) -> dict[str, Any] | None:
        rij = self.get("/tickets/search", query=f"number:{nummer}", limit=5, expand="true")
        return next((t for t in (rij if isinstance(rij, list) else [])
                     if str(t.get("number")) == str(nummer)), None)

    def email(self, user_id: int) -> str:
        return str(self.get(f"/users/{user_id}").get("email") or "")

    def tags(self, ticket_id: int) -> list[str]:
        return list(self.get("/tags", object="Ticket", o_id=ticket_id).get("tags") or [])


def cato(*args: str) -> Any:
    r = subprocess.run([CATO, *args], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"cato {' '.join(args[:2])} exit {r.returncode}")
    return json.loads(r.stdout) if r.stdout.strip().startswith(("[", "{")) else r.stdout


# ---------- de ronde ----------

def ronde(z: Zammad, run_cato: Callable[..., Any], sinds: datetime, schrijven: bool) -> Counter:
    telling: Counter = Counter()
    for t in z.escalaties(sinds):
        telling["escalaties"] += 1
        domein = mail_domein(z.email(t["customer_id"]))
        reden = overslaan_reden(domein)
        bedrijf = None
        if reden is None:
            kand = run_cato("companies", "list", "--domain", domein, "--json",
                            "--fields", "id,domainName", "--limit", "50") or []
            bedrijf, reden = exact_bedrijf(kand, domein)
        if reden:
            telling[f"overgeslagen_{reden}"] += 1
            continue
        # Elke notitie die dit ticket al noemt telt, ook een handmatige aan een persoon
        # (de terugvulling van 24-09 schreef "(#59034)" in de titel).
        bestaand = run_cato("notes", "list", "--title", f"#{t['number']}", "--all", "--json",
                            "--fields", "id,title") or []
        if any(noemt_ticket(n.get("title"), t["number"]) for n in bestaand):
            telling["al_genoteerd"] += 1
            continue
        titel, body = notitie(t, z.tags(t["id"]))
        if not schrijven:
            telling["zou_aanmaken"] += 1
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body)
        try:
            run_cato("notes", "create", "--soort", "KLANTMELDING", "--afhandeling", "OPEN",
                     "--title", titel, "--body-file", fh.name, "--company-id", bedrijf["id"],
                     "--no-dry-run", "--yes", "--json")
        finally:
            os.unlink(fh.name)
        telling["aangemaakt"] += 1

    # Terugkoppeling: eigen OPEN notities → OPGELOST als het ticket dicht is.
    eigen = run_cato("notes", "list", "--title", "Zammad #", "--soort", "KLANTMELDING", "--open",
                     "--all", "--json", "--fields", "id,title,bodyV2") or []
    for n in eigen:
        if HANDTEKENING not in ((n.get("bodyV2") or {}).get("markdown") or ""):
            continue                     # handwerk van een mens: niet aankomen
        nummer = ticketnummer_uit(n.get("title") or "")
        ticket = z.ticket_op_nummer(nummer) if nummer else None
        if not ticket or str(ticket.get("state") or "") not in GESLOTEN_STATES:
            continue
        if not schrijven:
            telling["zou_oplossen"] += 1
            continue
        run_cato("notes", "update", n["id"], "--afhandeling", "OPGELOST",
                 "--no-dry-run", "--yes", "--json")
        telling["opgelost"] += 1
    return telling


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="YYYY-MM-DD; standaard 14 dagen terug")
    ap.add_argument("--no-dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    schrijven = a.no_dry_run and a.yes
    if a.no_dry_run != a.yes:
        print("--no-dry-run en --yes horen samen", file=sys.stderr)
        return 2
    sinds = (datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since
             else datetime.now(timezone.utc) - TERUGKIJKEN)
    try:
        token = open(TOKEN_FILE).read().strip()
        telling = ronde(Zammad(ZAMMAD_BASE, token), cato, sinds, schrijven)
    except Exception as e:               # alleen de foutklasse: geen URL's of inhoud
        print(f"zammad-cato-meldingen: FOUT {type(e).__name__}", file=sys.stderr)
        return 1
    print(("" if schrijven else "[droogloop] ") + "zammad-cato-meldingen sinds "
          f"{sinds:%Y-%m-%d}: " + ", ".join(f"{k} {v}" for k, v in sorted(telling.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
