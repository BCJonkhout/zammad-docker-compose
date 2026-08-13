#!/usr/bin/env python3
"""Tel de KB-artikelen met kapotte opmaak. Read-only.

Draai: /usr/bin/python3 /root/zammad/bin/kb-format-check.py
Exit 0 = alles schoon, exit 1 = er staat nog kapotte opmaak in de kennisbank.

Dit meet de bodies zoals Zammad ze ná sanitizing bewaart -- dus wat de klant
werkelijk ziet, niet wat het script dacht te sturen.
"""
import importlib.util
import re
import sys

spec = importlib.util.spec_from_file_location("ds", "/root/zammad/bin/docs-sync.py")
ds = importlib.util.module_from_spec(spec)
sys.modules["ds"] = ds
spec.loader.exec_module(ds)

DEFECTS = {
    "streepjes-tabel": r"<(p|li)>[^<]*\|",
    "los uitroepteken": r"!\s*<a href=|!\[",
    "letterlijk >": r"<(p|li)>\s*&gt;",
}


def main() -> int:
    client = ds.ZammadClient(
        "https://support.prudai.com",
        open("/root/zammad/secrets/docs-sync.token").read().strip(),
    )
    totals = {name: 0 for name in DEFECTS}
    checked = 0
    for kb_id in (1, 2):
        assets = ds.get_kb_snapshot(client, kb_id)
        locale = ds.get_kb_locale_id(assets, kb_id)
        categories, _ = ds.build_category_state(assets, locale, kb_id)
        answers, _ = ds.build_answer_state(assets, locale, allowed_category_ids=set(categories))
        for answer in answers.values():
            if not answer.managed:
                continue
            checked += 1
            for name, pattern in DEFECTS.items():
                if re.search(pattern, answer.body or ""):
                    totals[name] += 1
                    print(f"  KAPOT [{name}] KB{kb_id} {answer.slug}")
    print(f"{checked} beheerde artikelen gecontroleerd")
    for name, count in totals.items():
        print(f"  {name:20} {count}")
    return 1 if any(totals.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
