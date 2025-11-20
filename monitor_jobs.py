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
# If you prefer webdriver-manager, you can swap to the Service version instead.
# from selenium.webdriver.chrome.service import Service
# from webdriver_manager.chrome import ChromeDriverManager

# ===============================
# Configuration
# ===============================

GS_URL = (
    "https://higher.gs.com/results?"
    "EXPERIENCE_LEVEL=Analyst|Associate"
    "&JOB_FUNCTION=Software%20Engineering"
    "&LOCATION=San%20Francisco|Wilmington|West%20Palm%20Beach|Atlanta|Chicago|Boston|"
    "Jersey%20City|Albany|New%20York|Dallas|Houston|Richardson|Draper|Salt%20Lake%20City"
    "&page=1&sort=POSTED_DATE"
)

PAYPAL_URL = (
    "https://paypal.eightfold.ai/careers?"
    "domain=paypal.com&Codes=W-LINKEDIN&start=0&location=United+States"
    "&pid=274915946441&sort_by=timestamp&filter_include_remote=1"
    "&filter_job_category=Software+Engineering"
)

MS_URL = (
    "https://apply.careers.microsoft.com/careers?"
    "start=0"
    "&location=United+States"
    "&pid=1970393556621281"
    "&sort_by=timestamp"
    "&filter_include_remote=1"
    "&filter_employment_type=full-time"
    "&filter_roletype=individual+contributor"
    "&filter_profession=software+engineering"
)

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


def is_ms_relevant_title(title: str) -> bool:
    """
    For Microsoft results, only keep Software Engineer or Software Engineer II
    and ignore senior, principal, manager, lead, architect, intern roles.
    """
    t = title.lower()

    exclude_tokens = [
        "senior",
        "principal",
        "director",
        "architect",
        "manager",
        "lead",
        "intern",
        "internship",
    ]
    if any(token in t for token in exclude_tokens):
        return False

    allowed_prefixes = [
        "software engineer ii",
        "software engineer 2",
        "software engineer",
    ]
    return any(t.startswith(prefix) for prefix in allowed_prefixes)


def start_browser() -> webdriver.Chrome:
    """Start headless Chrome via Selenium Manager (no webdriver-manager needed)."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Selenium Manager finds a matching driver automatically:
    return webdriver.Chrome(options=opts)
    # If you prefer webdriver-manager:
    # service = Service(ChromeDriverManager().install())
    # return webdriver.Chrome(service=service, options=opts)


def absolute(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)


# ===============================
# Site scrapers
# ===============================


def scrape_gs(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    source = "Goldman Sachs"
    base = "https://higher.gs.com"

    driver.get(GS_URL)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/roles/"]'))
        )
    except Exception:
        pass

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
    source = "PayPal"
    base = "https://paypal.eightfold.ai"

    driver.get(PAYPAL_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/careers/job"]')
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/jobs/"]')
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[data-ph-id]')
                ),
            )
        )
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls = set()

    anchors = soup.select(
        'a[href*="/careers/job"], a[href*="/jobs/"], a[data-ph-id]'
    )
    for a in anchors:
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        looks_like_job = (
            "/careers/job" in href or "/job/" in href or "/jobs/" in href
        )
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


def scrape_ms(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    source = "Microsoft"
    base = "https://apply.careers.microsoft.com"

    driver.get(MS_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'a[href*="/careers/job/"]')
            )
        )
    except Exception:
        # If the wait fails we still try to parse whatever is present
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls = set()

    anchors = soup.select('a[href*="/careers/job/"]')
    for a in anchors:
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)

        if not href or not title:
            continue

        if is_excluded(title):
            continue
        if not is_ms_relevant_title(title):
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
        parts.append(f"<h4>{source}</h4>")
        parts.append("<ul>")
        for url, title in items:
            parts.append(
                f'<li><a href="{url}" target="_blank" rel="noopener">{title}</a></li>'
            )
        parts.append("</ul>")
    return "\n".join(parts)


def send_email(new_items: list[tuple[str, str, str]]) -> None:
    if not new_items:
        return

    grouped: dict[str, list[tuple[str, str]]] = {}
    for source, url, title in new_items:
        grouped.setdefault(source, []).append((url, title))

    user = os.getenv("EMAIL_USER")
    pwd = os.getenv("EMAIL_PASSWORD")
    if not user or not pwd:
        print("EMAIL_USER/EMAIL_PASSWORD not set; skipping email.")
        return

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"New job postings ({len(new_items)}) - {now_utc}"

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
    items: list[tuple[str, str, str]] = []
    try:
        items.extend(scrape_gs(driver))
    except Exception as exc:
        print(f"[WARN] GS scrape error: {exc}")

    try:
        items.extend(scrape_paypal(driver))
    except Exception as exc:
        print(f"[WARN] PayPal scrape error: {exc}")

    try:
        items.extend(scrape_ms(driver))
    except Exception as exc:
        print(f"[WARN] Microsoft scrape error: {exc}")

    return items


def run_once() -> None:
    seen = load_seen_jobs()
    driver = start_browser()
    try:
        all_items = fetch_all(driver)
        new_items = [
            (src, url, title) for (src, url, title) in all_items if url not in seen
        ]
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
    driver = start_browser()
    try:
        all_items = fetch_all(driver)
        unique_urls = [url for (_, url, _) in all_items]
        seen_set = set()
        init_list: list[str] = []
        for u in unique_urls:
            if u not in seen_set:
                seen_set.add(u)
                init_list.append(u)
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
    parser = argparse.ArgumentParser(
        description="Monitor job postings and email new ones."
    )
    parser.add_argument(
        "--run-once",
        dest="run_once",
        action="store_true",
        help="Run a single check and exit.",
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Record current postings to seen_jobs.txt without emailing.",
    )
    args = parser.parse_args()

    if args.initialize:
        initialize_seen()
        return
    if args.run_once:
        run_once()
        return

    # If run locally without flags, loop forever
    while True:
        run_once()
        print(f"Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
