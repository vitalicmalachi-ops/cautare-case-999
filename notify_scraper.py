#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cautare Case 999.md - Notificator Telegram (pentru rulare automata)
=======================================================================

Varianta "fara interfata" a scraperului, gandita sa ruleze singura,
periodic, intr-un mediu automatizat (GitHub Actions). La fiecare rulare:

  1. Citeste criteriile din config.json (regiune, sub-zona, pret, teren).
  2. Cauta anunturile care corespund criteriilor (aceeasi logica ca in
     aplicatia cu interfata grafica).
  3. Compara cu state.json (anunturile deja vazute la rulari anterioare).
  4. Trimite pe Telegram DOAR anunturile noi, pe care nu le-a mai vazut.
  5. Actualizeaza state.json cu anunturile nou vazute.

Config necesar (variabile de mediu, setate ca "secrets" in GitHub):
  TELEGRAM_BOT_TOKEN - token-ul botului tau de Telegram
  TELEGRAM_CHAT_ID   - id-ul conversatiei tale cu botul

RULARE LOCALA (pentru testare, optional):
  pip install -r requirements.txt
  playwright install chromium
  set TELEGRAM_BOT_TOKEN=...   (sau export pe Mac/Linux)
  set TELEGRAM_CHAT_ID=...
  python notify_scraper.py
"""

import os
import re
import sys
import json
import time
import unicodedata

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
MAX_SEEN_IDS_KEPT = 3000  # ca sa nu creasca fisierul la nesfarsit

BASE_URL = "https://999.md/ro/list/real-estate/house-and-garden"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

REGIONS = {
    "Chișinău mun.": ("Chișinău mun.", "chisinau"),
    "Bălți mun.": ("Bălți mun.", "balti"),
    "Orhei": ("Orhei", "orhei"),
    "Ungheni": ("Ungheni", "ungheni"),
    "Ialoveni": ("Ialoveni", "ialoveni"),
}


# ---------------------------------------------------------------------------
# Aceleasi functii de extragere ca in aplicatia cu interfata grafica
# ---------------------------------------------------------------------------

def strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


CYRILLIC_LOOKALIKES = str.maketrans({
    "а": "a", "А": "A", "е": "e", "Е": "E", "о": "o", "О": "O",
    "р": "p", "Р": "P", "с": "c", "С": "C", "у": "y", "У": "Y",
    "х": "x", "Х": "X",
})


def normalize_lookalikes(text: str) -> str:
    return text.translate(CYRILLIC_LOOKALIKES)


LAND_LABEL_PATTERN = re.compile(
    r"suprafata teren\w*\D{0,15}?([\d]+(?:[.,]\d+)?)\s*(ari|ar|ha|hectare)\b"
)
LAND_FALLBACK_LATIN = [
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*ari\b"), 1.0),
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*ar\b"), 1.0),
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*hectare\b"), 100.0),
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*ha\b"), 100.0),
]
LAND_FALLBACK_CYRILLIC = [
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*(?:соток|сотки|сотка|сот)\b"), 1.0),
    (re.compile(r"([\d]+(?:[.,]\d+)?)\s*га\b"), 100.0),
]
REGION_LINE_PATTERN = re.compile(r"([^\n,]+?)\s+mun\.,")
ADDRESS_LINE_PATTERN = re.compile(r"^(.+mun\.,.*)$", re.MULTILINE)


def parse_land_ari(full_text_lower: str):
    latin_norm = strip_diacritics(normalize_lookalikes(full_text_lower))
    m = LAND_LABEL_PATTERN.search(latin_norm)
    if m:
        val = float(m.group(1).replace(",", "."))
        factor = 100.0 if m.group(2) in ("ha", "hectare") else 1.0
        return val * factor
    for pattern, factor in LAND_FALLBACK_LATIN:
        m = pattern.search(latin_norm)
        if m:
            return float(m.group(1).replace(",", ".")) * factor
    for pattern, factor in LAND_FALLBACK_CYRILLIC:
        m = pattern.search(full_text_lower)
        if m:
            return float(m.group(1).replace(",", ".")) * factor
    return None


def matches_subzone(details: dict, subzone_label: str) -> bool:
    if not subzone_label or subzone_label.startswith("Toate"):
        return True
    target = strip_diacritics(subzone_label)
    zona_norm = strip_diacritics(details.get("zona") or "")
    adresa_norm = strip_diacritics(details.get("adresa") or "")
    return target == zona_norm or target in adresa_norm


def fetch_ad_details(session, ad_id: str):
    url = f"https://999.md/ro/{ad_id}"
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = re.sub(r"\s*\|\s*999\.md\s*$", "", soup.title.string.strip())
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else f"Anunt {ad_id}"

    price = None
    currency = None
    meta_price = soup.find("meta", attrs={"property": "product:price:amount"})
    meta_currency = soup.find("meta", attrs={"property": "product:price:currency"})
    if meta_price and meta_price.get("content"):
        try:
            price = float(meta_price["content"])
        except ValueError:
            price = None
    if meta_currency and meta_currency.get("content"):
        currency = meta_currency["content"].upper()

    full_text = soup.get_text(separator="\n")
    full_text_lower = full_text.lower()

    region_match = REGION_LINE_PATTERN.search(full_text)
    region = strip_diacritics(region_match.group(1).strip()) if region_match else None

    zona = ""
    adresa = ""
    addr_match = ADDRESS_LINE_PATTERN.search(full_text)
    if addr_match:
        full_line = addr_match.group(1).strip()
        tokens = [t.strip() for t in full_line.split(",") if t.strip()]
        if len(tokens) >= 3:
            zona = tokens[2]
        elif len(tokens) == 2:
            zona = tokens[1]
        adresa = ", ".join(tokens[1:]) if len(tokens) > 1 else full_line

    land_ari = parse_land_ari(full_text_lower)

    return {
        "ad_id": ad_id, "url": url, "title": title,
        "price": price, "currency": currency,
        "region": region, "land_ari": land_ari,
        "zona": zona, "adresa": adresa,
    }


def build_page_url(base_url: str, page_num: int) -> str:
    if page_num <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page_num}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  Eroare Telegram: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"  Eroare la trimiterea pe Telegram: {e}")


# ---------------------------------------------------------------------------
# Config + stare (anunturi deja vazute)
# ---------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Logica principala
# ---------------------------------------------------------------------------

def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("EROARE: lipsesc TELEGRAM_BOT_TOKEN sau TELEGRAM_CHAT_ID din variabilele de mediu.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})
    region_label = config.get("region_label", "Chișinău mun.")
    subzone_label = config.get("subzone_label", "Toate (mun. Chișinău)")
    min_price = float(config.get("min_price", 0))
    max_price = float(config.get("max_price", 100000))
    min_land = float(config.get("min_land", 4))
    max_land = config.get("max_land")
    max_land = float(max_land) if max_land not in (None, "") else None
    max_pages = int(config.get("max_pages", 100))

    region_click_text, region_expected = REGIONS[region_label]

    state = load_json(STATE_PATH, {"seen_ids": []})
    seen_ids = set(state.get("seen_ids", []))

    print(f"Criterii: regiune={region_label}, sub-zona={subzone_label}, "
          f"pret {min_price}-{max_price} EUR, teren {min_land}-{max_land} ari.")
    print(f"Anunturi deja cunoscute din rulari anterioare: {len(seen_ids)}")

    print("PASUL 1: colectare ID-uri anunturi din lista...")
    ad_ids = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        page = browser.new_page(locale="ro-RO")

        page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1000)
        try:
            page.get_by_text(region_click_text, exact=True).first.click(timeout=8000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(1500)
            print(f"Filtru regiune aplicat. URL: {page.url}")
        except Exception as e:
            print(f"Nu am putut da click pe filtrul de regiune ({e}). Continui oricum.")

        filtered_base_url = page.url
        seen_this_run = set()
        for page_num in range(1, max_pages + 1):
            url = build_page_url(filtered_base_url, page_num)
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1200)

            hrefs = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href'))
                    .filter(h => h && /^\\/ro\\/\\d+/.test(h))
                """
            )
            added = 0
            for h in hrefs:
                m = re.match(r"^/ro/(\d+)", h)
                if m and m.group(1) not in seen_this_run:
                    seen_this_run.add(m.group(1))
                    ad_ids.append(m.group(1))
                    added += 1
            print(f"  [Lista {page_num}] {added} anunturi noi ({len(ad_ids)} total).")
            if added == 0:
                break
            time.sleep(1.0)

        browser.close()

    print(f"Total anunturi de verificat: {len(ad_ids)}")
    print("PASUL 2: verificare detaliata si trimitere notificari...")

    session = requests.Session()
    new_matches = 0
    for i, ad_id in enumerate(ad_ids, start=1):
        details = fetch_ad_details(session, ad_id)
        time.sleep(0.5)
        if details is None:
            continue

        if details["region"] != region_expected:
            continue
        if not matches_subzone(details, subzone_label):
            continue
        if (details["price"] is None or details["currency"] != "EUR"
                or details["price"] < min_price or details["price"] > max_price):
            continue
        if details["land_ari"] is None or details["land_ari"] < min_land:
            continue
        if max_land is not None and details["land_ari"] > max_land:
            continue

        # A trecut de toate criteriile - e o potrivire. Notificam DOAR daca
        # nu am mai vazut acest anunt la rulari anterioare.
        if ad_id in seen_ids:
            continue

        new_matches += 1
        text = (
            f"🏠 <b>{details['title']}</b>\n"
            f"💰 {details['price']:.0f} EUR\n"
            f"📐 {details['land_ari']} ari"
            + (f" — {details['zona']}" if details['zona'] else "") + "\n"
            f"🔗 {details['url']}"
        )
        print(f"  NOU: {details['url']}")
        send_telegram_message(bot_token, chat_id, text)
        seen_ids.add(ad_id)

    # Adaugam si restul anunturilor vazute in aceasta rulare (chiar daca nu
    # au corespuns criteriilor), ca sa nu le re-analizam degeaba data viitoare
    # -- de fapt le lasam neadaugate intentionat, ca sa fie re-verificate
    # (preturile se pot schimba). Pastram doar ID-urile care AU corespuns
    # criteriilor si au fost deja notificate.
    if len(seen_ids) > MAX_SEEN_IDS_KEPT:
        seen_ids = set(list(seen_ids)[-MAX_SEEN_IDS_KEPT:])

    save_json(STATE_PATH, {"seen_ids": sorted(seen_ids)})

    print(f"\nGata! {new_matches} anunturi noi trimise pe Telegram.")


if __name__ == "__main__":
    main()
