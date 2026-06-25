import json, os, re, smtplib, sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PRODUCT = json.loads(Path("product.json").read_text(encoding="utf-8"))
MARKETS = json.loads(Path("markets.json").read_text(encoding="utf-8"))
STATE_FILE = Path("state.json")

POSITIVE = re.compile(r"(abholbar|verfügbar|reservierbar|in\s+2\s+stunden|warenkorb|online\s+bestellen|lieferung\s+nach\s+hause|bestand)", re.I)
NEGATIVE = re.compile(r"(nicht\s+verfügbar|zur\s+zeit\s+leider\s+nicht|ausverkauft|nicht\s+bestellbar|derzeit\s+nicht|momentan\s+nicht)", re.I)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def short_text(text, n=1200):
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def classify(text):
    """Sehr vorsichtige Ampel. True nur bei eindeutig positiven Hinweisen."""
    t = re.sub(r"\s+", " ", text)
    if POSITIVE.search(t) and not NEGATIVE.search(t[:2500]):
        return "MÖGLICHERWEISE_VERFÜGBAR"
    if NEGATIVE.search(t):
        return "NICHT_VERFÜGBAR"
    return "UNKLAR"


def accept_cookies(page):
    for pattern in ["Alle akzeptieren", "Akzeptieren", "Zustimmen", "OK"]:
        try:
            page.get_by_role("button", name=re.compile(pattern, re.I)).click(timeout=1500)
            return
        except Exception:
            pass


def try_select_market(page, market):
    """Versucht, im Toom-Shop den Markt zu setzen. Wenn Toom die Seite ändert, bleibt ein Fallback."""
    # 1) Erst Marktseite öffnen; manche Shops setzen dabei Cookie/Session für den Markt.
    try:
        page.goto(market["market_url"], wait_until="networkidle", timeout=45000)
        accept_cookies(page)
        try:
            page.get_by_text(re.compile("Als.*Mein Markt|Mein Markt festlegen", re.I)).click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
    except Exception:
        pass

    # 2) Produktseite öffnen.
    page.goto(PRODUCT["url"], wait_until="networkidle", timeout=45000)
    accept_cookies(page)

    # 3) Falls ein Marktauswahl-Dialog existiert, versuchen wir ihn zu benutzen.
    for label in ["Anderen Markt auswählen", "Mein Markt", "Markt auswählen", "Markt ändern", "Verfügbarkeit in anderen Märkten"]:
        try:
            page.get_by_text(re.compile(label, re.I)).first.click(timeout=2500)
            page.wait_for_timeout(1000)
            break
        except Exception:
            pass

    # Suchfeld befüllen, falls vorhanden.
    try:
        inp = page.locator("input").filter(has_text="").first
        inp.fill(market["search"], timeout=3000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        # Ergebnis mit Marktname anklicken.
        page.get_by_text(re.compile(market["name"].split("-")[0], re.I)).first.click(timeout=3000)
        page.wait_for_timeout(2000)
    except Exception:
        pass


def check_market(browser, market):
    page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36")
    try:
        try_select_market(page, market)
        page.wait_for_timeout(2000)
        text = page.locator("body").inner_text(timeout=10000)
        status = classify(text)
        return {"market": market["name"], "status": status, "excerpt": short_text(text), "url": PRODUCT["url"]}
    except PlaywrightTimeoutError as e:
        return {"market": market["name"], "status": "FEHLER", "excerpt": f"Timeout: {e}", "url": PRODUCT["url"]}
    except Exception as e:
        return {"market": market["name"], "status": "FEHLER", "excerpt": repr(e), "url": PRODUCT["url"]}
    finally:
        page.close()


def send_email(subject, body):
    sender = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("ALERT_TO", sender)
    if not sender or not password or not recipient:
        print("E-Mail nicht eingerichtet: SMTP_USER/SMTP_PASSWORD/ALERT_TO fehlen.")
        print(body)
        return
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def main():
    state = load_state()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for market in MARKETS:
            result = check_market(browser, market)
            print(result["market"], result["status"])
            results.append(result)
        browser.close()

    now = datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")
    changed = []
    available = []
    for r in results:
        old = state.get(r["market"])
        if old != r["status"]:
            changed.append((old, r))
        if r["status"] == "MÖGLICHERWEISE_VERFÜGBAR":
            available.append(r)
        state[r["market"]] = r["status"]
    save_state(state)

    # Mail nur bei Verfügbarkeit oder Statusänderung. Beim allerersten Lauf kommt eine Statusmail.
    if available:
        subject = "🎉 PortaSplit möglicherweise verfügbar!"
        body = f"Stand: {now}\n\nMidea PortaSplit, Toom-Art.-Nr. {PRODUCT['article_number']}\n{PRODUCT['url']}\n\n"
        body += "Positive Treffer:\n" + "\n".join(f"- {r['market']}: {r['status']}" for r in available)
        body += "\n\nBitte sofort Toom-Seite/App prüfen oder im Markt anrufen. Der Monitor ist vorsichtig, aber nicht rechtsverbindlich/verbindlich für Bestand."
        send_email(subject, body)
    elif changed:
        subject = "PortaSplit-Monitor: Statusänderung"
        body = f"Stand: {now}\n\nStatusänderungen:\n"
        for old, r in changed:
            body += f"- {r['market']}: {old or 'neu'} → {r['status']}\n"
        body += f"\nLink: {PRODUCT['url']}\n"
        send_email(subject, body)
    else:
        print("Keine Änderung; keine Mail.")

if __name__ == "__main__":
    main()
