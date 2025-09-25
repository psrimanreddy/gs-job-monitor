"""
Job Posting Monitor for higher.gs.com
------------------------------------

This script periodically checks a customized Goldman Sachs careers search
page hosted on ``higher.gs.com`` for new job postings. When the script
detects one or more new postings that have not been seen before, it
sends an email alert containing the new job titles and links. The
script is designed to run in the background on a schedule and uses a
headless browser (via Selenium) to render the site since the page
relies heavily on JavaScript.

Usage
~~~~~

1. Install the required Python packages:

   ``pip install selenium webdriver-manager beautifulsoup4``

2. Ensure that Google Chrome is installed on the machine where the
   script will run. The ``webdriver-manager`` package will
   automatically download the appropriate version of Chromedriver.

3. Create a Google account app password for sending email via Gmail.
   Regular account passwords will not work if two‑factor authentication
   is enabled. Instructions for creating an app password are available
   from Google's support documentation.

4. Set the following environment variables before running the script:

   - ``EMAIL_USER`` – your Gmail address (the sender address)
   - ``EMAIL_PASSWORD`` – the app password created in step 3

   On Linux or macOS you can export these variables in a terminal
   session like this::

       export EMAIL_USER="youraddress@gmail.com"
       export EMAIL_PASSWORD="yourapppassword"

5. Update the ``TARGET_URL`` constant below with your own search URL.
   You can construct a URL by visiting the Goldman Sachs careers site
   (higher.gs.com), applying the desired filters, and copying the
   resulting address from the browser's address bar.

6. Run the script::

       python monitor_jobs.py

   The script will check the page every 30 minutes by default. If
   new postings appear, an email will be dispatched to the address
   specified in ``NOTIFICATION_RECIPIENTS``.

Persistent State
~~~~~~~~~~~~~~~~

The script keeps track of previously seen job identifiers in a local
file named ``seen_jobs.txt`` placed in the same directory as this
script. If you wish to reset the monitor and re‑alert on all
currently available postings, simply delete this file.

Disclaimer
~~~~~~~~~~

This script is provided as a convenience to monitor job postings on
higher.gs.com. It may require adjustments if the structure of the
site changes or if additional authentication measures are put in
place. Use at your own discretion.
"""

import os
import time
import smtplib
import argparse
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# URL to monitor. Replace with your customized search URL.
# The default provided here filters for Analyst and Associate roles in
# Software Engineering across specific U.S. locations, sorted by posted date.
TARGET_URL = (
    "https://higher.gs.com/results?EXPERIENCE_LEVEL=Analyst|Associate"
    "&JOB_FUNCTION=Software%20Engineering"
    "&LOCATION=San%20Francisco|Wilmington|West%20Palm%20Beach|Atlanta|Chicago"
    "|Boston|Jersey%20City|Albany|New%20York|Dallas|Houston|Richardson|Draper|Salt%20Lake%20City"
    "&page=1&sort=POSTED_DATE"
)

# List of email addresses to notify when new jobs are found. Feel free to
# add multiple recipients separated by commas.
NOTIFICATION_RECIPIENTS = [
    "psrimanreddy@gmail.com",
]

# Interval between checks (in seconds). 1800 seconds = 30 minutes.
CHECK_INTERVAL = 1800

# Optional keyword filters for job titles. Only titles containing at least
# one of the keywords will be considered. Modify this list to narrow
# notifications to specific roles. By default, we focus on Software
# Engineering roles.
TITLE_KEYWORDS = ["Software", "Engineer", "Engineering"]

# File to persist IDs of jobs that have already triggered notifications.
SEEN_FILE = "seen_jobs.txt"


def load_seen_jobs() -> set[str]:
    """Load previously seen job identifiers from the persistence file.

    Returns a set containing the relative URLs (job identifiers) of
    postings that have already been processed.
    """
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                job_id = line.strip()
                if job_id:
                    seen.add(job_id)
    return seen


def save_new_jobs(job_ids: list[str]) -> None:
    """Append new job identifiers to the persistence file."""
    if not job_ids:
        return
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        for job_id in job_ids:
            f.write(job_id + "\n")


def get_current_postings() -> list[tuple[str, str]]:
    """Fetch the current list of job postings from the target URL.

    Because higher.gs.com relies on client‑side rendering, we use a
    headless Chrome browser to load the page. After the page finishes
    loading, we parse the DOM to extract job titles and their relative
    links. Each link typically follows the pattern ``/roles/{roleId}``.

    Returns a list of tuples containing the job identifier (relative URL)
    and job title.
    """
    # Configure Selenium to run headless. Add additional arguments for
    # performance and reliability in environments without a display.
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")

    # Initialize the driver using webdriver-manager to download the
    # appropriate version of Chromedriver if not already present.
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        driver.get(TARGET_URL)

        # Wait for dynamic content to load. Adjust this sleep as
        # necessary—too short and jobs may not appear; too long and the
        # script wastes time. Ten seconds is usually sufficient.
        time.sleep(10)

        html = driver.page_source
    finally:
        driver.quit()

    soup = BeautifulSoup(html, "html.parser")

    postings: list[tuple[str, str]] = []
    # Search for anchor tags that link to individual roles. These links
    # typically reside under ``/roles/{roleId}``. We include both
    # ``href`` and the visible text as the job title.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/roles/"):
            title = a.get_text(strip=True)
            if not title:
                continue
            # Apply keyword filtering if TITLE_KEYWORDS is defined. If any
            # keyword appears in the title (case-insensitive), include it.
            if TITLE_KEYWORDS:
                lower_title = title.lower()
                if not any(keyword.lower() in lower_title for keyword in TITLE_KEYWORDS):
                    continue
            postings.append((href, title))
    return postings


def send_email(new_jobs: list[tuple[str, str]]) -> None:
    """Send an email notification for newly detected jobs.

    The email will contain a list of the new job titles with links to
    their respective postings. Credentials are read from environment
    variables ``EMAIL_USER`` and ``EMAIL_PASSWORD``. Both must be set
    before running the script.
    """
    email_user = os.environ.get("EMAIL_USER")
    email_password = os.environ.get("EMAIL_PASSWORD")

    if not email_user or not email_password:
        print("Error: EMAIL_USER and EMAIL_PASSWORD environment variables must be set.")
        return

    msg = MIMEMultipart()
    msg["From"] = email_user
    msg["To"] = ", ".join(NOTIFICATION_RECIPIENTS)
    msg["Subject"] = (
        f"New Goldman Sachs jobs posted on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    body_lines = [
        "The following new job postings were detected on higher.gs.com:",
        "",
    ]
    for job_id, title in new_jobs:
        full_url = f"https://higher.gs.com{job_id}"
        body_lines.append(f"- {title}: {full_url}")
    body = "\n".join(body_lines)
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_user, email_password)
            server.sendmail(email_user, NOTIFICATION_RECIPIENTS, msg.as_string())
        print(f"Sent notification for {len(new_jobs)} new jobs.")
    except Exception as exc:
        print(f"Failed to send email: {exc}")


def run_once() -> None:
    """
    Perform a single check for new job postings and send notifications.

    This helper is used when running the script in a scheduled
    environment like GitHub Actions. It loads the existing set of
    previously seen jobs, fetches the current postings, sends an email
    for any newly discovered jobs, updates the persistence file, and
    then exits.
    """
    seen_jobs = load_seen_jobs()
    try:
        postings = get_current_postings()
        new_jobs = [(job_id, title) for job_id, title in postings if job_id not in seen_jobs]

        if new_jobs:
            send_email(new_jobs)
            save_new_jobs([job_id for job_id, _ in new_jobs])
    except Exception as exc:
        print(f"Error during monitoring: {exc}")


def initialize_seen_jobs() -> None:
    """
    Populate ``seen_jobs.txt`` with all current postings without sending any emails.

    This helper is useful when you want to start monitoring without being alerted
    about all existing jobs. It fetches the current postings, writes their
    identifiers to the persistence file, and exits.
    """
    try:
        postings = get_current_postings()
        job_ids = [job_id for job_id, _ in postings]
        # Overwrite the file to ensure a clean start
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            for jid in job_ids:
                f.write(jid + "\n")
        print(f"Initialized seen jobs file with {len(job_ids)} postings.")
    except Exception as exc:
        print(f"Error during initialization: {exc}")


def main() -> None:
    """
    Entry point for the script.

    If invoked with the ``--run-once`` command-line option, the script
    will perform a single check for new postings and then exit. This
    mode is intended for scheduled environments (e.g., GitHub Actions).

    Otherwise, the script runs continuously, checking the target URL at
    the interval specified by ``CHECK_INTERVAL``.
    """
    parser = argparse.ArgumentParser(description="Monitor Goldman Sachs job postings.")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Perform a single run of the monitor instead of looping."
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Populate the seen jobs file with current postings without sending email and exit."
    )
    args = parser.parse_args()

    if args.initialize:
        initialize_seen_jobs()
        return

    if args.run_once:
        run_once()
        return

    # Continuous monitoring loop
    seen_jobs = load_seen_jobs()
    print(f"Loaded {len(seen_jobs)} previously seen jobs.")

    while True:
        try:
            postings = get_current_postings()
            new_jobs = [(job_id, title) for job_id, title in postings if job_id not in seen_jobs]

            if new_jobs:
                send_email(new_jobs)
                save_new_jobs([job_id for job_id, _ in new_jobs])
                seen_jobs.update(job_id for job_id, _ in new_jobs)
        except Exception as exc:
            # Catch all exceptions to prevent the loop from stopping.
            print(f"Error during monitoring: {exc}")

        # Print next scheduled run for logging purposes
        next_time = datetime.now() + timedelta(seconds=CHECK_INTERVAL)
        print(
            f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. "
            f"Next check scheduled at {next_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
