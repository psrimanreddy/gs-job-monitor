import os
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# -----------------------
# CONFIGURATION
# -----------------------
GS_URL = (
    "https://higher.gs.com/results?"
    "EXPERIENCE_LEVEL=Analyst|Associate"
    "&JOB_FUNCTION=Software%20Engineering"
    "&LOCATION=San%20Francisco|Wilmington|West%20Palm%20Beach|Atlanta|Chicago|Boston|Jersey%20City|Albany|New%20York|Dallas|Houston|Richardson|Draper|Salt%20Lake%20City"
    "&page=1&sort=POSTED_DATE"
)
PAYPAL_URL = (
    "https://paypal.eightfold.ai/careers?"
    "domain=paypal.com&Codes=W-LINKEDIN&start=0&location=United+States"
    "&pid=274915946441&sort_by=timestamp&filter_include_remote=1"
    "&filter_job_category=Software+Engineering"
)
EXCLUDED_KEYWORDS = [
    "staff",
    "manager",
    "site reliability",
    "sre",
    "mobile",
    "ios",
]

CHECK_INTERVAL = 1800  # 30 minutes, used only in manual runs
SEEN_FILE = "seen_jobs.txt"


# -----------------------
# HELPER FUNCTIONS
# -----------------------
def load_seen_jobs():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_new_jobs(job_ids):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        for job_id in job_ids:
            f.write(f"{job_id}\n")


def is_excluded(title: str) -> bool:
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in EXCLUDED_KEYWORDS)


# -----------------------
# SCRAPING FUNCTIONS
# -----------------------
def setup_browser():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
    return driver


def get_gs_postings(browser):
    postings = []
    browser.get(GS_URL)
    time.sleep(5)
    soup = BeautifulSoup(browser.page_source, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/roles/"):
            title = a.get_text(strip=True)
            if title and not is_excluded(title):
                postings.append((href, title))
    return postings


def get_paypal_postings(browser):
    postings = []
    browser.get(PAYPAL_URL)
    time.sleep(5)
    soup = BeautifulSoup(browser.page_source, "html.parser")
    for job_card in soup.find_all("a", href=True):
        href = job_card["href"]
        title = job_card.get_text(strip=True)
        if "job" in href.lower() and title and not is_excluded(title):
            postings.append((href, title))
    return postings


# -----------------------
# EMAIL NOTIFICATION
# -----------------------
def send_email(new_jobs):
    if not new_jobs:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"New Job Postings ({len(new_jobs)})"
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = os.getenv("EMAIL_USER")

    html_content = "<h3>New Job Postings</h3><ul>"
    for url, title in new_jobs:
        full_link = url if url.startswith("http") else f"https://higher.gs.com{url}"
        html_content += f'<li><a href="{full_link}" target="_blank">{title}</a></li>'
    html_content += "</ul>"

    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASSWORD"))
            server.sendmail(msg["From"], [msg["To"]], msg.as_string())
        print(f"Email sent with {len(new_jobs)} new jobs.")
    except Exception as e:
        print("Failed to send email:", e)


# -----------------------
# MAIN LOGIC
# -----------------------
def run_once():
    seen = load_seen_jobs()
    browser = setup_browser()
    try:
        postings = []
        postings.extend(get_gs_postings(browser))
        postings.extend(get_paypal_postings(browser))

        new_jobs = []
        for job_id, title in postings:
            if job_id not in seen:
                new_jobs.append((job_id, title))

        if new_jobs:
            send_email(new_jobs)
            save_new_jobs([jid for jid, _ in new_jobs])
        else:
            print("No new jobs found.")
    finally:
        browser.quit()


if __name__ == "__main__":
    run_once()
