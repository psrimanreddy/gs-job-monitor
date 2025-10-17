#!/usr/bin/env python3
import os
import sys
import time
import smtplib
import argparse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# If you prefer using webdriver-manager, uncomment the next two lines and the Service block below.
# from selenium.webdriver.chrome.service import Service
# from webdriver_manager.chrome import ChromeDriverManager


# ===============================
# Configuration
# ===============================

# Goldman Sachs filtered URL (Software Engineering, selected locations)
GS_URL = (
    "https://higher.gs.com/results?"
    "EXPERIENCE_LEVEL=Analyst|Associate"
    "&JOB_FUNCTION=Software%20Engineering"
    "&LOCATION=San%20Francisco|Wilmington|West%20Palm%20Beach|Atlanta|Chicago|Boston|"
    "Jersey%20City|Albany|New%20York|Dallas|Houston|Richardson|Draper|Salt%20Lake%20City"
    "&page=1&sort=POSTED_DATE"
)

# PayPal (Eightfold) filtered URL
PAYPAL_URL = (
    "https://paypal.eightfold.ai/careers?"
    "domain=paypal.com&Codes=W-LINKEDIN&start=0&location=United+States"
    "&pid=274915946441&sort_by=timestamp&filter_include_remote=1"
    "&filter_job_category=Software+Engineering"
)

# Exclude titles containing these (case-insensitive)
EXCLUDED_KEYWORDS = [
    "staff",
    "manager",
    "site reliability",
    "sre",
    "mobile",
    "ios",
]

SEEN_FILE = "seen_jobs.txt"
CHECK_INTERVAL = 1800  # only used if you run locally without flags


# ===============================
# Utilities
# ===============================

def load_seen_jobs() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_new_jobs(job_ids: list[str]) -> None:
    if not job_ids:
        return
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        for jid in job_ids:
            f.write(jid + "\n")


def is_excluded(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in EXCLUDED_KEYWORDS)


def start_browser() -> webdriver.Chrome:
    """Start headless Chrome via Selenium Manager (no webdriver-manager needed)."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # If you prefer webdriver-manager instead of Selenium Manager:
    # service = Service(ChromeDriverManager().install())
    # return webdriver.Chrome(service=service, options=opts)
    return webdriver.Chrome(options=opts)


def absolute(base: str, href: str) -> str:
    """Make a full URL from base+href when href is relative."""
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)


# ===============================
# Site scrapers
# ===============================

def scrape_gs(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """
    Return a list of tuples (source, job_id, title_or_text_link).
    job_id is the canonical URL; source is 'Goldman Sachs'.
    """
    source = "Goldman Sachs"
    base = "https://higher.gs.com"

    driver.get(GS_URL)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/roles/"]'))
        )
    except Exception:
        pass  # fall through and parse whatever is available

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls = set()

    for a in soup.select('a[href^="/roles/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not title or is_excluded(title):
            continue
        url = absolute(base, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append((source, url, title))

    return results


def scrape_paypal(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """
    Return a list of tuples (source, job_id, title).
    job_id is the canonical URL; source is 'PayPal'.
    """
    source = "PayPal"
    base = "https://paypal.eightfold.ai"

    driver.get(PAYPAL_URL)
    # Wait for job anchors. Eightfold DOM varies; cover a few common patterns.
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/careers/job"]')),
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/jobs/"]')),
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[data-ph-id]'))
            )
        )
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls = set()

    # Try a few selectors; Eightfold often uses /careers/job/<id> links.
    anchors = soup.select('a[href*="/careers/job"], a[href*="/jobs/"], a[data-ph-id]')
    for a in anchors:
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        # Heuristic: ignore empty titles, generic nav links, etc.
        if not href or not title:
            continue
        # basic de-dup heuristics: want things that look like job pages
        looks_like_job = ("/careers/job" in href) or ("/job/" in href) or ("/jobs/" in href)
        if not looks_like_job:
            continue
        if is_excluded(title):
            continue

        url = absolute(base, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append((source, url, title))

    return results


# ===============================
# Email
# ===============================

def format_email_html(grouped: dict[str, list[tuple[str, str]]]) -> str:
    parts = ['<h3>New Job Postings</h3>']
    for source, items in grouped.items():
        if not items:
            continue
        parts.append(f'<h4>{source}</h4>')
        parts.append("<ul>")
        for url, title in items:
            parts.append(f'<li><a href="{url}" target="_blank" rel="noopener">{title}</a></li>')
        parts.append("</ul>")
    return "\n".join(parts)


def send_email(new_items: list[tuple[str, str, str]]) -> None:
    """new_items: list of (source, url, title)."""
    if not new_items:
        return

    # group by source for readability
    grouped: dict[str, list[tuple[str, str]]] = {}
    for source, url, title in new_items:
        grouped.setdefault(source, []).append((url, title))

    user = os.getenv("EMAIL_USER")
    pwd = os.getenv("EMAIL_PASSWORD")
    if not user or not pwd:
        print("EMAIL_USER/EMAIL_PASSWORD not set; skipping email.")
        return

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"New job postings ({len(new_items)}) – {now_utc}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = user

    html = format_email_html(grouped)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pwd)
            server.sendmail(user, [user], msg.as_string())
        print(f"Email sent with {len(new_items)} new jobs.")
    except Exception as exc:
        print(f"Failed to send email: {exc}")


# ===============================
# Orchestration
# ===============================

def fetch_all(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """Return [(source, url, title), ...] from all monitored sites."""
    items: list[tuple[str, str, str]] = []
    try:
        items.extend(scrape_gs(driver))
    except Exception as exc:
        print(f"[WARN] GS scrape error: {exc}")
    try:
        items.extend(scrape_paypal(driver))
    except Exception as exc:
        print(f"[WARN] PayPal scrape error: {exc}")
    return items


def run_once() -> None:
    seen = load_seen_jobs()
    driver = start_browser()
    try:
        all_items = fetch_all(driver)
        # Only those whose URL (job_id) hasn't been seen
        new_items = [(src, url, title) for (src, url, title) in all_items if url not in seen]
        if new_items:
            send_email(new_items)
            save_new_jobs([url for (_, url, _) in new_items])
        else:
            print("No new jobs.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def initialize_seen() -> None:
    """Populate seen_jobs.txt with everything currently visible (no email)."""
    driver = start_browser()
    try:
        all_items = fetch_all(driver)
        unique_urls = [url for (_, url, _) in all_items]
        # de-dup while preserving order
        seen_set = set()
        init_list: list[str] = []
        for u in unique_urls:
            if u not in seen_set:
                seen_set.add(u)
                init_list.append(u)
        # overwrite file with current snapshot
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            for u in init_list:
                f.write(u + "\n")
        print(f"Initialized {SEEN_FILE} with {len(init_list)} items.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor job postings and email new ones.")
    parser.add_argument("--run-once", action="store_true", help="Run a single check and exit.")
    parser.add_argument("--initialize", action="store_true",
                        help="Record current postings to seen_jobs.txt without emailing.")
    args = parser.parse_args()

    if args.initialize:
        initialize_seen()
        return

    if args.run-once:
        run_once()
        return

    # If run locally without flags, loop forever
    while True:
        run_once()
        print(f"Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
