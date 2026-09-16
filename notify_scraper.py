#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cautare Case 999.md - Notificator Telegram (v3: oprire optimizata paginare)
================================================================================

Nou fata de v2:
  - Optimizare de viteza: la fiecare rulare, pastram acum lista completa de
    ID-uri de anunturi VAZUTE in lista (nu doar cele care au corespuns
    criteriilor), separat per categorie+regiune, in state.json.
    Deoarece 999.md sorteaza anunturile descrescator dupa data postarii,
    daca o pagina intreaga contine DOAR ID-uri deja cunoscute dintr-o
    rulare anterioara, inseamna ca am ajuns la anunturi vechi si NU mai
    are rost sa continuam paginarea - o oprim imediat.
    Asta reduce rularile ulterioare (dupa prima, care ramane completa)
    de la 20-30 minute la 1-2 minute.

Restul functionalitatii e identic cu v2:
  - Notificari catre mai multi useri (telegram_chat_ids in config.json).
  - Mai multe "profiluri" de cautare (case / terenuri, cu criterii proprii).
  - Cauta in categoria corecta a site-ului (CATEGORY_URLS), plus o plasa
    de siguranta pentru anunturile mis-categorizate in cealalta categorie.

Vezi config.json pentru formatul exact.
"""

import os
import re
import sys
import json
import time
import unicodedata
import concurrent.futures

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CONFIG_PATH = os.environ.get("SCRAPER_CONFIG_PATH", "config.json")
STATE_PATH = os.environ.get("SCRAPER_STATE_PATH", "state.json")
MAX_SEEN_IDS_KEPT_PER_PROFILE = 3000
MAX_SEEN_LIST_IDS_KEPT = 6000  # per categorie+regiune, pentru optimizarea de paginare

# 999.md tine casele si terenurile in DOUA categorii separate pe site.
CATEGORY_URLS = {
    "casa": "https://999.md/ro/list/real-estate/house-and-garden",
    "teren": "https://999.md/ro/list/real-estate/land",
    "oricare": "https://999.md/ro/list/real-estate/house-and-garden",
}
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
    "Criuleni": ("Criuleni", "criuleni"),
}


# ---------------------------------------------------------------------------
# Extragere date din pagina fiecarui anunt
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

PROPERTY_TYPE_PATTERN = re.compile(
    r"\btip\b[:\s]{0,4}(casa|teren|vila|townhouse|duplex|apartament)\b"
)


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


def detect_property_type(full_text_latin_norm: str) -> str:
    m = PROPERTY_TYPE_PATTERN.search(full_text_latin_norm)
    if not m:
        return "necunoscut"
    val = m.group(1)
    return "teren" if val == "teren" else "casa"


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
    full_text_latin_norm = strip_diacritics(normalize_lookalikes(full_text_lower))

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
    property_type = detect_property_type(full_text_latin_norm)

    return {
        "ad_id": ad_id, "url": url, "title": title,
        "price": price, "currency": currency,
        "region": region, "land_ari": land_ari,
        "zona": zona, "adresa": adresa,
        "property_type": property_type,
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
            print(f"  Eroare Telegram (chat {chat_id}): {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"  Eroare la trimiterea pe Telegram (chat {chat_id}): {e}")


def notify_all(bot_token: str, chat_ids, text: str):
    for chat_id in chat_ids:
        send_telegram_message(bot_token, str(chat_id).strip(), text)


# ---------------------------------------------------------------------------
# Config + stare
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
# Colectare ID-uri de anunturi pentru o categorie+regiune (Playwright)
# ---------------------------------------------------------------------------

def collect_ad_ids_for_region(page, base_url: str, region_label: str, max_pages: int, known_ids: set):
    region_click_text, _ = REGIONS[region_label]

    page.goto(base_url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)
    try:
        page.get_by_text(region_click_text, exact=True).first.click(timeout=8000)
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(1500)
        print(f"  Filtru regiune '{region_label}' aplicat. URL: {page.url}")
    except Exception as e:
        print(f"  Nu am putut da click pe filtrul de regiune '{region_label}' ({e}). Continui oricum.")

    filtered_base_url = page.url
    ad_ids = []
    seen_this_run = set()
    consecutive_fully_known_pages = 0
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
        page_ids = []
        for h in hrefs:
            m = re.match(r"^/ro/(\d+)", h)
            if m:
                page_ids.append(m.group(1))

        added = 0
        for ad_id in page_ids:
            if ad_id not in seen_this_run:
                seen_this_run.add(ad_id)
                ad_ids.append(ad_id)
                added += 1

        print(f"    [Lista {page_num}] {added} anunturi noi ({len(ad_ids)} total).")

        # OPTIMIZARE (cu marja de siguranta): 999.md permite promovarea
        # contra cost a anunturilor, care le aduce din nou in fata listei
        # chiar daca nu sunt noi. Asta inseamna ca o SINGURA pagina plina
        # doar cu anunturi cunoscute nu garanteaza ca tot ce urmeaza e
        # vechi - un anunt chiar nou ar putea fi impins mai jos de unul
        # promovat. De aceea cerem DOUA pagini la rand complet cunoscute
        # inainte sa oprim paginarea.
        if page_ids and all(pid in known_ids for pid in page_ids):
            consecutive_fully_known_pages += 1
            print(f"    Pagina complet cunoscuta ({consecutive_fully_known_pages}/2 la rand).")
            if consecutive_fully_known_pages >= 2:
                print("    Doua pagini la rand complet cunoscute - opresc paginarea (optimizare).")
                break
        else:
            consecutive_fully_known_pages = 0

        if added == 0:
            print("    Nu mai sunt anunturi noi, am ajuns probabil la ultima pagina.")
            break

        time.sleep(1.0)

    return ad_ids


# ---------------------------------------------------------------------------
# Logica principala
# ---------------------------------------------------------------------------

def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("EROARE: lipseste TELEGRAM_BOT_TOKEN din variabilele de mediu.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})

    chat_ids = config.get("telegram_chat_ids")
    if not chat_ids:
        single = os.environ.get("TELEGRAM_CHAT_ID")
        chat_ids = [single] if single else []
    if not chat_ids:
        print("EROARE: nu exista niciun chat_id de Telegram (config.json > telegram_chat_ids).")
        sys.exit(1)
    print(f"Notificarile vor fi trimise catre {len(chat_ids)} persoana(e).")

    profiles = config.get("profiles")
    if not profiles:
        print("EROARE: config.json trebuie sa aiba o lista 'profiles' cu cel putin un profil.")
        sys.exit(1)

    state = load_json(STATE_PATH, {"seen": {}, "seen_list_ids": {}})
    seen_by_profile = state.get("seen", {})
    seen_list_ids_state = state.get("seen_list_ids", {})

    # Pasul 1: colectam ID-urile de anunturi o singura data per
    # (categorie, regiune) - profilurile care cauta in aceeasi categorie
    # SI aceeasi regiune refolosesc aceeasi lista. Pentru fiecare
    # combinatie, folosim ID-urile cunoscute din rularile anterioare ca
    # sa oprim paginarea mai devreme.
    print("PASUL 1: colectare ID-uri anunturi, per categorie+regiune...")
    ad_ids_by_key = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        page = browser.new_page(locale="ro-RO")

        for profile in profiles:
            region_label = profile.get("region_label", "Chișinău mun.")
            max_pages = int(profile.get("max_pages", 100))
            property_type = profile.get("property_type", "oricare")
            base_url = CATEGORY_URLS.get(property_type, CATEGORY_URLS["oricare"])
            key = (base_url, region_label, max_pages)
            if key in ad_ids_by_key:
                continue

            state_key = f"{base_url}|{region_label}"
            known_ids = set(seen_list_ids_state.get(state_key, []))
            print(f"Colectare din '{base_url}' pentru regiune='{region_label}' "
                  f"(max {max_pages} pagini, {len(known_ids)} deja cunoscute)...")

            collected = collect_ad_ids_for_region(page, base_url, region_label, max_pages, known_ids)
            ad_ids_by_key[key] = collected

            updated_known = known_ids | set(collected)
            if len(updated_known) > MAX_SEEN_LIST_IDS_KEPT:
                updated_known = set(list(updated_known)[-MAX_SEEN_LIST_IDS_KEPT:])
            seen_list_ids_state[state_key] = sorted(updated_known)

        browser.close()

    all_ad_ids = set()
    for ids in ad_ids_by_key.values():
        all_ad_ids.update(ids)
    print(f"\nTotal anunturi unice de verificat (toate profilurile): {len(all_ad_ids)}")

    # Pasul 2: descarcam detaliile fiecarui anunt UNIC, o singura data, in paralel.
    print("PASUL 2: verificare detaliata (in paralel)...")
    session = requests.Session()
    details_cache = {}
    checked = 0
    total = len(all_ad_ids)
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_id = {
            executor.submit(fetch_ad_details, session, ad_id): ad_id
            for ad_id in all_ad_ids
        }
        for future in concurrent.futures.as_completed(future_to_id):
            checked += 1
            if checked % 50 == 0 or checked == total:
                print(f"  Verificate {checked}/{total}...")
            ad_id = future_to_id[future]
            details_cache[ad_id] = future.result()

    # Pasul 3: aplicam criteriile FIECARUI profil.
    #
    # Sursa principala e categoria corecta de pe site (de incredere). Dar
    # oamenii mai greseesc categoria cand posteaza un anunt - ca sa nu
    # ratam un teren postat din greseala printre case (sau invers),
    # verificam si "cealalta" categorie, si acceptam de-acolo DOAR
    # anunturile al caror titlu chiar pare sa fie tipul cautat (plasa
    # de siguranta bazata pe text, nu sursa principala).
    print("\nPASUL 3: aplicare criterii per profil si notificare...")

    ids_by_region_any_category = {}
    for (b_url, r_label, m_pages), ids in ad_ids_by_key.items():
        ids_by_region_any_category.setdefault(r_label, set()).update(ids)

    total_new = 0
    for profile in profiles:
        label = profile.get("label", "Cautare")
        region_label = profile.get("region_label", "Chișinău mun.")
        subzone_label = profile.get("subzone_label", "Toate (mun. Chișinău)")
        property_type = profile.get("property_type", "oricare")
        min_price = float(profile.get("min_price", 0))
        max_price = float(profile.get("max_price", 100000))
        min_land = float(profile.get("min_land", 4))
        max_land = profile.get("max_land")
        max_land = float(max_land) if max_land not in (None, "") else None
        max_pages = int(profile.get("max_pages", 100))

        _, region_expected = REGIONS[region_label]
        base_url = CATEGORY_URLS.get(property_type, CATEGORY_URLS["oricare"])
        own_ids = set(ad_ids_by_key.get((base_url, region_label, max_pages), []))

        other_ids = ids_by_region_any_category.get(region_label, set()) - own_ids
        stray_ids = {
            ad_id for ad_id in other_ids
            if details_cache.get(ad_id) and details_cache[ad_id].get("property_type") == property_type
        }

        candidate_ids = own_ids | stray_ids
        seen_ids = set(seen_by_profile.get(label, []))

        new_for_profile = 0
        for ad_id in candidate_ids:
            details = details_cache.get(ad_id)
            if details is None:
                continue
        # Verificarea de mai jos e o plasa suplimentara, bazata pe textul
        # paginii. Regex-ul care extrage regiunea presupune formatul
        # "X mun.," - valabil pentru Chisinau/Balti, dar NU pentru raioane
        # ca Orhei, Ungheni, Ialoveni, Criuleni, unde adresele nu contin
        # acel format. Daca nu am reusit sa extragem regiunea din text
        # (details["region"] este None), NU respingem anuntul - avem deja
        # incredere in filtrul de regiune aplicat direct pe site la
        # colectare (Pasul 1).
        if details["region"] is not None and details["region"] != region_expected:
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
            if ad_id in seen_ids:
                continue

            new_for_profile += 1
            stray_tag = " (categorie diferita)" if ad_id in stray_ids else ""
            text = (
                f"🏷️ <b>{label}</b>{stray_tag}\n"
                f"🏠 {details['title']}\n"
                f"💰 {details['price']:.0f} EUR\n"
                f"📐 {details['land_ari']} ari"
                + (f" — {details['zona']}" if details['zona'] else "") + "\n"
                f"🔗 {details['url']}"
            )
            print(f"  [{label}] NOU: {details['url']}{stray_tag}")
            notify_all(bot_token, chat_ids, text)
            seen_ids.add(ad_id)

        if len(seen_ids) > MAX_SEEN_IDS_KEPT_PER_PROFILE:
            seen_ids = set(list(seen_ids)[-MAX_SEEN_IDS_KEPT_PER_PROFILE:])
        seen_by_profile[label] = sorted(seen_ids)
        print(f"  Profil '{label}': {new_for_profile} anunturi noi.")
        total_new += new_for_profile

    save_json(STATE_PATH, {"seen": seen_by_profile, "seen_list_ids": seen_list_ids_state})
    print(f"\nGata! {total_new} anunturi noi trimise pe Telegram, in total.")


if __name__ == "__main__":
    main()
