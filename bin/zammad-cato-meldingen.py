#!/usr/bin/env python3
"""Zammad-escalatie → KLANTMELDING-notitie in CATO (kaart 70bbb1f0, punt 2).

    /usr/bin/python3 bin/zammad-cato-meldingen.py                 # droogloop: zegt wat hij zou doen
    /usr/bin/python3 bin/zammad-cato-meldingen.py --no-dry-run --yes
    /usr/bin/python3 bin/zammad-cato-meldingen.py --since 2026-09-01   # verder terugkijken

Wat het doet:
1. Zoekt in Zammad de tickets met tag `ai-escalate` (gezet door zammad-autoreply
   als de AI-triage een ticket naar een medewerker escaleert), bijgewerkt sinds
   `--since` (standaard 14 dagen terug), plus oudere tickets die nog open staan en
   sindsdien zijn bijgewerkt (een vervolgbericht dat later escaleert). Zoeken gaat via een SQL-conditie,
   níet via de Elasticsearch-zoekopdracht: die geeft bij een ES-fout een lege lijst
   met HTTP 200 terug, en de live index miste op 20-10-2026 één van de 17 tickets.
2. Koppelt elk ticket aan een CATO-bedrijf via het e-maildomein van de klant: alleen
   bij precies één bedrijf met exact dat domein als **primair** domein is het een
   "bekende klantorganisatie" (`cato companies list --domain` zoekt alleen daarop).
   Gratis mail, Prudai zelf, geen of meerdere treffers → overslaan (geteld, niet
   geraden). Zammad-organisaties helpen hier niet: op 20-10-2026 had 1 van de 16
   geëscaleerde tickets er een.
3. Noemt nog geen enkele CATO-notitie `#<ticketnummer>` in de titel (ook handwerk,
   aan welk bedrijf of persoon ook), dan maakt hij er een aan via de cato-CLI: soort KLANTMELDING, afhandeling OPEN.
4. Notities die dít script eerder maakte en nog OPEN staan, zet hij op OPGELOST zodra
   het ticket in Zammad gesloten of samengevoegd is (ticket gelezen op id uit de link
   in de notitie, rechtstreeks uit de database) — anders blijft de groom elke
   week vragen naar meldingen die allang klaar zijn.

Waarom een host-script en niet in zammad-autoreply zelf: het enige schrijfpad naar
CATO is de cato-CLI (AGENTS.md, "nooit rauwe REST-writes"), en die draait op de host,
niet in de container. Bijkomend: de webhook blijft precies zoals hij is, en uitrollen
vraagt geen herstart van een productiedienst — alleen een systemd-timer.

Vertrouwelijkheid: de notitie bevat alleen metadata — ticketnummer + link, datum,
categorie- en signaaltags. Geen onderwerp, geen berichttekst, geen namen; de inhoud
blijft in Zammad. De uitvoer van dit script telt alleen (geen domeinen of namen).

Fouten: één ticket dat misgaat stopt de rest niet; het telt als `fout_<klasse>` en de
run eindigt met exit 1, zodat systemd hem als mislukt toont.

Leest Zammad met het bestaande autoreply-token (ticket.agent; tickets zoeken,
gebruikers en tags lezen). Schrijft niets in Zammad. Gebeurtenisdatum in de titel =
aanmaakdatum van het ticket (op 20-10-2026 viel de escalatietag bij alle 17 tickets op
die dag; een vervolgbericht dat later escaleert krijgt dus de datum van het ticket)."""
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
PAGINA = 100
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


def primair_domein(company: dict[str, Any]) -> str:
    """Alleen het primaire domein: daar zoekt `cato companies list --domain` op, dus
    een bedrijf dat alleen via een secundair domein bekend is, vinden we niet."""
    return host((company.get("domainName") or {}).get("primaryLinkUrl"))


def exact_bedrijf(kandidaten: list[dict[str, Any]], domein: str) -> tuple[dict | None, str | None]:
    """`cato companies list --domain` zoekt op substring ('ab.nl' vindt ook 'grab.nl');
    hier telt alleen een exacte host. Eén treffer = bekend; anders een reden."""
    exact = [c for c in kandidaten if primair_domein(c) == domein]
    if len(exact) == 1:
        return exact[0], None
    return None, ("geen_bedrijf" if not exact else "meerdere_bedrijven")


def nl_datum(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%d-%m-%Y")


def eerder_regel(eerder: list[dict[str, Any]]) -> str:
    """`cato guide` §7: noem eerdere meldingen/incidenten van deze relatie."""
    if not eerder:
        return "Eerder: geen meldingen of incidenten vastgelegd."
    laatste = max(n["createdAt"] for n in eerder)
    open_n = sum(1 for n in eerder if n.get("afhandeling") == "OPEN")
    return (f"Eerder: {len(eerder)} meldingen/incidenten, laatste vastgelegd {nl_datum(laatste)}"
            + (f", waarvan {open_n} nog open." if open_n else "."))


def notitie(ticket: dict[str, Any], tags: list[str],
            eerder: list[dict[str, Any]] | None = None) -> tuple[str, str]:
    """Titel + body, alleen metadata. De datum vooraan in de titel is wat
    /cato-agenda en /cato-groom als gebeurtenisdatum lezen; de eerste bodyregel
    `Bron: Zammad #…` is de vorm die `cato guide` §7 voorschrijft."""
    nummer, datum = ticket["number"], nl_datum(ticket["created_at"])
    categorie = next((t[len("topic-"):] for t in sorted(tags) if t.startswith("topic-")), None)
    signalen = sorted(t[len("escalation-"):] for t in tags if t.startswith("escalation-"))
    titel = f"Klantmelding {datum} — Zammad #{nummer} geëscaleerd"
    body = "\n".join([
        f"Bron: Zammad #{nummer}",
        "",
        f"Zammad-ticket [#{nummer}]({ZAMMAD_PUBLIC}/#ticket/zoom/{ticket['id']}) is door de "
        f"AI-triage geëscaleerd naar een medewerker (ticket aangemaakt {datum}).",
        "",
        f"- Categorie: {categorie or 'onbekend'}",
        f"- Signalen: {', '.join(signalen) if signalen else 'geen'}",
        f"- {eerder_regel(eerder or [])}",
        "",
        "De inhoud staat in Zammad en hoort niet in CATO. Afhandeling springt vanzelf op "
        "OPGELOST zodra het ticket in Zammad gesloten is.",
        "",
        f"_{HANDTEKENING}_",
    ])
    return titel, body


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def in_venster(t: dict[str, Any], sinds: datetime) -> bool:
    """Aangemaakt sinds `sinds`, óf ouder maar nog open en sindsdien bijgewerkt (een
    vervolgbericht dat later escaleert). Een oud ticket dat alleen gesloten werd,
    valt erbuiten — anders maakt de eerste run een stapel oude meldingen aan."""
    if _ts(t["created_at"]) >= sinds:
        return True
    return _ts(t["updated_at"]) >= sinds and str(t.get("state") or "") not in GESLOTEN_STATES


def ticket_id_uit(body: str) -> int | None:
    """Het Zammad-ticket-id uit de link die notitie() in de body zet."""
    m = re.search(r"#ticket/zoom/(\d+)\)", body or "")
    return int(m.group(1)) if m else None


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

    def post(self, path: str, body: dict[str, Any]) -> Any:
        req = urllib.request.Request(f"{self.base}{path}", data=json.dumps(body).encode(),
                                     method="POST",
                                     headers={"Authorization": f"Token token={self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def escalaties(self, sinds: datetime) -> list[dict[str, Any]]:
        """Alle tickets met de escalatietag, via de SQL-conditie (geen `query` → Zammad
        slaat Elasticsearch over). Een antwoord dat geen lijst is, is een fout — nooit
        stil "geen escalaties"."""
        uit, pagina = [], 1
        while True:
            rij = self.post("/tickets/search", {
                "condition": {"ticket.tags": {"operator": "contains all",
                                              "value": ESCALATIE_TAG}},
                "limit": PAGINA, "page": pagina, "expand": True})
            if not isinstance(rij, list):
                raise RuntimeError("zoekantwoord is geen lijst")
            uit += rij
            if len(rij) < PAGINA:
                break
            pagina += 1
        return [t for t in uit if in_venster(t, sinds)]

    def ticket(self, ticket_id: int) -> dict[str, Any]:
        return self.get(f"/tickets/{ticket_id}", expand="true")

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
        try:
            verwerk_ticket(z, run_cato, t, schrijven, telling)
        except Exception as e:          # één ticket stopt de rest niet
            telling[f"fout_{type(e).__name__}"] += 1
    telling.update(sync_opgelost(z, run_cato, schrijven))
    return telling


def verwerk_ticket(z: Zammad, run_cato: Callable[..., Any], t: dict[str, Any],
                   schrijven: bool, telling: Counter) -> None:
    domein = mail_domein(z.email(t["customer_id"]))
    reden = overslaan_reden(domein)
    bedrijf = None
    if reden is None:
        kand = run_cato("companies", "list", "--domain", domein, "--all", "--json") or []
        bedrijf, reden = exact_bedrijf(kand, domein)
    if reden:
        telling[f"overgeslagen_{reden}"] += 1
        return
    # Elke notitie die dit ticket al noemt telt, ook een handmatige aan een persoon
    # (de terugvulling van 24-09 schreef "(#59034)" in de titel).
    bestaand = run_cato("notes", "list", "--title", f"#{t['number']}", "--all", "--json",
                        "--fields", "id,title") or []
    if any(noemt_ticket(n.get("title"), t["number"]) for n in bestaand):
        telling["al_genoteerd"] += 1
        return
    if not schrijven:
        telling["zou_aanmaken"] += 1
        return
    eerder = run_cato("notes", "list", "--soort", "KLANTMELDING,INCIDENT", "--company-id",
                      bedrijf["id"], "--all", "--json") or []
    titel, body = notitie(t, z.tags(t["id"]), eerder)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
    # Al dicht in Zammad (bv. bij terugvullen met --since): meteen OPGELOST vastleggen.
    afhandeling = "OPGELOST" if str(t.get("state") or "") in GESLOTEN_STATES else "OPEN"
    try:
        run_cato("notes", "create", "--soort", "KLANTMELDING", "--afhandeling", afhandeling,
                 "--title", titel, "--body-file", fh.name, "--company-id", bedrijf["id"],
                 "--no-dry-run", "--yes", "--json")
    finally:
        os.unlink(fh.name)
    telling["aangemaakt" if afhandeling == "OPEN" else "aangemaakt_opgelost"] += 1


def sync_opgelost(z: Zammad, run_cato: Callable[..., Any], schrijven: bool) -> Counter:
    """Eigen OPEN notities → OPGELOST als het ticket in Zammad dicht is."""
    telling: Counter = Counter()
    eigen = run_cato("notes", "list", "--title", "Zammad #", "--soort", "KLANTMELDING", "--open",
                     "--all", "--json", "--fields", "id,title,bodyV2") or []
    for n in eigen:
        body = (n.get("bodyV2") or {}).get("markdown") or ""
        if HANDTEKENING not in body:
            continue                     # handwerk van een mens: niet aankomen
        tid = ticket_id_uit(body)
        try:
            if tid is None:
                raise LookupError("geen ticketlink")
            ticket = z.ticket(tid)
        except Exception as e:           # telt mee in de exitcode, stopt de rest niet
            telling[f"fout_sync_{type(e).__name__}"] += 1
            continue
        if str(ticket.get("state") or "") not in GESLOTEN_STATES:
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
    sinds = (datetime.combine(datetime.strptime(a.since, "%Y-%m-%d").date(),
                              datetime.min.time(), TZ) if a.since
             else datetime.now(timezone.utc) - TERUGKIJKEN)
    try:
        token = open(TOKEN_FILE).read().strip()
        telling = ronde(Zammad(ZAMMAD_BASE, token), cato, sinds, schrijven)
    except Exception as e:               # alleen de foutklasse: geen URL's of inhoud
        print(f"zammad-cato-meldingen: FOUT {type(e).__name__}", file=sys.stderr)
        return 1
    telling.setdefault("escalaties", 0)  # "0" zegt iets; een lege regel niet
    print(("" if schrijven else "[droogloop] ") + "zammad-cato-meldingen sinds "
          f"{sinds:%Y-%m-%d}: " + ", ".join(f"{k} {v}" for k, v in sorted(telling.items())))
    return 1 if any(k.startswith("fout") for k in telling) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
