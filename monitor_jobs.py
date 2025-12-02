#!/usr/bin/env python3
"""
Monitor job postings across several company career sites and email new ones.

This script scrapes career pages for Goldman Sachs, PayPal, Microsoft,
Google, and Meta, filters roles to only the relevant Software Engineer
positions, and sends an email when new postings are found.  It keeps
track of previously seen postings in a simple text file (`seen_jobs.txt`).

The scraping logic for each site has been tailored based on manual
inspection via a browser:

* Goldman Sachs careers site exposes job detail links under the path
  `/roles/<id>`.  We request the first page of results sorted by
  posting date and then extract these links.
* PayPal’s careers site (powered by Eightfold) dynamically loads
  postings.  We look for anchors containing either `/careers/job`,
  `/jobs/` or the `data-ph-id` attribute.
* Microsoft careers pages serve job details under
  `/careers/job/<id>`.  We capture those anchors and filter titles to
  only keep Software Engineer or Software Engineer II roles, excluding
  Senior, Principal, Lead, Director, etc.
* Google Careers search lists jobs with a “Learn more” button.
  Clicking this button navigates to a job detail page whose URL
  contains `/results/<job-id>-<slug>`.  The script uses Selenium to
  click each “Learn more” button, record the resulting URL and title,
  and then navigates back to the search results.
* Meta careers search pages list software-engineer roles with
  detail links under `/jobs/<id>`.  The scraper collects job links
  matching this pattern and filters titles to only Software
  Engineer roles (excluding Senior, Staff, Lead, etc.).

The script uses environment variables `EMAIL_USER` and
`EMAIL_PASSWORD` (typically GitHub Actions secrets) to send summary
emails via Gmail.  It can be run once (`--run-once`), initialize the
seen list (`--initialize`), or loop indefinitely (for local usage).
"""

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

# ===============================
# Configuration
# ===============================

# A simplified URL for Goldman Sachs careers.  Filtering by experience
# level and job function via query parameters proved unreliable; the
# site uses client-side filters.  We request the first page sorted by
# posted date and then filter titles in code.
GS_URL = "https://higher.gs.com/results?page=1&sort=POSTED_DATE"

# PayPal career search for Software Engineering roles in the United
# States.  Eightfold’s dynamic loading means only the domain and
# high-level filters appear in the URL.
PAYPAL_URL = (
    "https://paypal.eightfold.ai/careers?"
    "domain=paypal.com&Codes=W-LINKEDIN&start=0&location=United+States"
    "&pid=274915946441&sort_by=timestamp&filter_include_remote=1"
    "&filter_job_category=Software+Engineering"
)

# Microsoft careers base URL.  The new careers site renders all jobs
# dynamically and exposes detail links under `/careers/job/<id>`.  We
# no longer attempt to pre-filter via query parameters (e.g., pid,
# profession) because these change frequently and can break scraping.
# Instead, we load the base careers page and rely on title filtering
# in code to identify Software Engineer roles (II only).
MS_URL = "https://apply.careers.microsoft.com/careers"

# Google careers search for Software Engineer roles at early and mid
# levels.  The script clicks “Learn more” buttons on this page to
# capture job detail URLs.
GOOGLE_URL = (
    "https://www.google.com/about/careers/applications/jobs/results?"
    "target_level=MID&target_level=EARLY"
    "&employment_type=FULL_TIME"
    "&sort_by=date"
    "&location=United+States"
    "&q=%22Software%20Engineer%22"
)

# Meta careers search for Software Engineer roles in North America.
META_URL = (
    "https://www.metacareers.com/jobsearch?"
    "q=Software%20Engineer"
    "&sort_by_new=true"
    "&offices[0]=North%20America"
)

# Global excluded keywords (case-insensitive) – titles containing
# any of these substrings will be skipped.
EXCLUDED_KEYWORDS = [
    "staff",
    "manager",
    "site reliability",
    "sre",
    "mobile",
    "ios",
]

# File used to persist seen job URLs.  Each line should contain one
# job URL.  If the file does not exist, it will be created.
SEEN_FILE = "seen_jobs.txt"

# Only used if running locally in an infinite loop (not used in CI).
CHECK_INTERVAL = 1800  # seconds

# ===============================
# Utilities
# ===============================

def load_seen_jobs() -> set[str]:
    """Return a set of previously seen job URLs."""
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_new_jobs(job_ids: list[str]) -> None:
    """Append newly seen job URLs to the seen file."""
    if not job_ids:
        return
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        for jid in job_ids:
            f.write(jid + "\n")


def is_excluded(title: str) -> bool:
    """Return True if title contains any globally excluded keywords."""
    t = title.lower()
    return any(k in t for k in EXCLUDED_KEYWORDS)


def is_ms_relevant_title(title: str) -> bool:
    """
    Determine if a Microsoft job title is relevant.

    Only accept roles that are exactly Software Engineer or Software
    Engineer II (including numeric variants).  Exclude Senior,
    Principal, Manager, Lead, Architect, Intern, etc.
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

    # Only allow SE and SE II variants.  Do not accept level 3 and above.
    allowed_prefixes = [
        "software engineer ii",
        "software engineer 2",
        "software engineer",
    ]
    return any(t.startswith(prefix) for prefix in allowed_prefixes)


def is_google_relevant_title(title: str) -> bool:
    """
    Determine if a Google job title is relevant.

    Accept Software Engineer (no level) and levels II and III.
    Exclude Senior and above (including Staff, Principal, Manager,
    Director, etc.) and Intern roles.
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
        "staff",
    ]
    if any(token in t for token in exclude_tokens):
        return False

    allowed_prefixes = [
        "software engineer iii",
        "software engineer ii",
        "software engineer 3",
        "software engineer 2",
        "software engineer",
    ]
    return any(t.startswith(prefix) for prefix in allowed_prefixes)


def is_meta_relevant_title(title: str) -> bool:
    """
    Determine if a Meta job title is relevant.

    Accept only non‑senior Software Engineer roles.  Exclude titles
    containing Senior, Staff, Principal, Director, Lead, Manager,
    Architect, or Intern.  The presence of "Software Engineer" must
    appear somewhere in the title.
    """
    t = title.lower()
    if "software engineer" not in t:
        return False

    exclude_tokens = [
        "senior",
        "staff",
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

    return True


def start_browser() -> webdriver.Chrome:
    """
    Start a headless Chrome instance via Selenium Manager.

    Selenium Manager automatically provisions the appropriate driver
    binary.  Additional flags disable sandboxing and dev‑shm for
    better compatibility in container environments.
    """
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)


def absolute(base: str, href: str) -> str:
    """
    Compute an absolute URL given a base and a relative href.  If
    href is already absolute, return it unchanged.
    """
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)


# ===============================
# Site scrapers
# ===============================

def scrape_gs(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """Scrape Goldman Sachs careers for job links and titles."""
    source = "Goldman Sachs"
    base = "https://higher.gs.com"

    driver.get(GS_URL)
    try:
        # Wait until job cards are present.  Links to detail pages live
        # under /roles/<id>, but the page is dynamically rendered.
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/roles/"]'))
        )
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

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
    """Scrape PayPal careers for job links and titles."""
    source = "PayPal"
    base = "https://paypal.eightfold.ai"

    driver.get(PAYPAL_URL)
    try:
        # Wait until at least one job link is present.  Eightfold uses
        # dynamic rendering; anchors may have different patterns.
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/careers/job"]')
                ),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/jobs/"]')
                ),
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[data-ph-id]')),
            )
        )
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    anchors = soup.select(
        'a[href*="/careers/job"], a[href*="/jobs/"], a[data-ph-id]'
    )
    for a in anchors:
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        # Filter out non‑job links.  Accept if it appears to be a job.
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


def scrape_ms(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """Scrape Microsoft careers for relevant job links and titles."""
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
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    for a in soup.select('a[href*="/careers/job/"]'):
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        if is_excluded(title) or not is_ms_relevant_title(title):
            continue
        url = absolute(base, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append((source, url, title))

    return results


def scrape_google(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """
    Scrape Google careers search results.

    The Google job search interface lists positions with a “Learn more”
    button.  This scraper clicks each button, records the resulting
    job detail URL and title, filters titles (Software Engineer I/II/III),
    and then navigates back to the search page.
    """
    source = "Google"
    driver.get(GOOGLE_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[normalize-space()='Learn more'] | //a[normalize-space()='Learn more']")
            )
        )
    except Exception:
        return []

    results: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    # Google results are paginated via scroll; we process only first page
    # (typically ~20 jobs) to minimize runtime.  Adjust as needed.
    max_jobs = 30

    for idx in range(max_jobs):
        try:
            buttons = driver.find_elements(
                By.XPATH,
                "//button[normalize-space()='Learn more'] | //a[normalize-space()='Learn more']",
            )
            if idx >= len(buttons):
                break

            btn = buttons[idx]
            # Scroll into view to avoid click interception
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", btn
                )
            except Exception:
                pass

            # Extract title from nearest heading
            title = ""
            try:
                container = btn.find_element(By.XPATH, "ancestor::*[.//h2 or .//h3][1]")
                heading = container.find_element(By.XPATH, ".//h2 | .//h3")
                title = heading.text.strip()
            except Exception:
                title = ""

            if title:
                lower = title.lower()
                if "software engineer" not in lower:
                    continue
                if is_excluded(title) or not is_google_relevant_title(title):
                    continue

            old_url = driver.current_url
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                continue

            # Wait for the URL to change to a job detail page
            try:
                WebDriverWait(driver, 20).until(EC.url_changes(old_url))
                url = driver.current_url
            except Exception:
                # Navigation failed – go back and continue
                try:
                    driver.back()
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located(
                            (
                                By.XPATH,
                                "//button[normalize-space()='Learn more'] | "
                                "//a[normalize-space()='Learn more']",
                            )
                        )
                    )
                except Exception:
                    pass
                continue

            if not title:
                try:
                    heading = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, "//h1 | //h2"))
                    )
                    title = heading.text.strip()
                except Exception:
                    title = "Software Engineer (Google job)"

            if url in seen_urls:
                try:
                    driver.back()
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located(
                            (
                                By.XPATH,
                                "//button[normalize-space()='Learn more'] | "
                                "//a[normalize-space()='Learn more']",
                            )
                        )
                    )
                except Exception:
                    pass
                continue

            seen_urls.add(url)

            if (
                "software engineer" in title.lower()
                and is_google_relevant_title(title)
                and not is_excluded(title)
            ):
                results.append((source, url, title))

            # Navigate back to the results page for the next job
            try:
                driver.back()
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located(
                        (
                            By.XPATH,
                            "//button[normalize-space()='Learn more'] | "
                            "//a[normalize-space()='Learn more']",
                        )
                    )
                )
            except Exception:
                break

        except Exception:
            break

    return results


def scrape_meta(driver: webdriver.Chrome) -> list[tuple[str, str, str]]:
    """
    Scrape Meta careers search for relevant Software Engineer roles.

    Meta’s search page lists roles with anchors pointing to `/jobs/<id>`
    rather than the `/profile/job_details/` pattern used on detail pages.
    We parse the search results page, collect unique job links under
    `/jobs/`, and filter titles to only include non‑senior Software
    Engineer roles.  If Meta’s markup changes, adjust the selector
    accordingly.
    """
    source = "Meta"
    base = "https://www.metacareers.com"

    driver.get(META_URL)
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'a[href*="/jobs/"]')
            )
        )
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    # Job links on the search page use /jobs/<id>.  We capture these and
    # filter the titles.  Some roles such as "Senior" or "Staff" are
    # excluded via is_meta_relevant_title().
    anchors = soup.select('a[href*="/jobs/"]')
    for a in anchors:
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        if is_excluded(title) or not is_meta_relevant_title(title):
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
    """Return an HTML string summarizing new jobs grouped by source."""
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
    """
    Send an email summarizing new job postings.

    If no new items are provided, this function returns immediately.
    A grouping by source is performed so that the email is organized
    nicely.  Gmail SSL on port 465 is used to send the message.
    """
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
    """Fetch job postings from all configured sources."""
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
    try:
        items.extend(scrape_google(driver))
    except Exception as exc:
        print(f"[WARN] Google scrape error: {exc}")
    try:
        items.extend(scrape_meta(driver))
    except Exception as exc:
        print(f"[WARN] Meta scrape error: {exc}")
    return items


def run_once() -> None:
    """Perform a single scrape and email any new job postings."""
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
    """
    Initialize the seen jobs file with the current postings.

    This function discards previous `seen_jobs.txt` content and records
    the current job URLs so they will not trigger an email until new
    postings appear.
    """
    driver = start_browser()
    try:
        all_items = fetch_all(driver)
        unique_urls = [url for (_, url, _) in all_items]
        seen_set: set[str] = set()
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
