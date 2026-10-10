import os
import smtplib
from email.message import EmailMessage

import requests

# --- Your settings (change these anytime) ---
DEFAULT_ROLES = "business analyst fresher, data analyst fresher"
SEARCH_TEXT = os.environ.get("JOB_ROLE") or DEFAULT_ROLES  # read from GitHub setting JOB_ROLE
ROLES = [role.strip() for role in SEARCH_TEXT.split(",") if role.strip()]  # separate roles with commas
MAX_DAYS_OLD = 3          # only jobs posted in the last 3 days
JOBS_PER_ROLE = 25        # how many jobs to fetch for each role

# --- Secrets (GitHub gives these to the script, they are not written here) ---
ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")


def fetch_jobs_for_role(role):
    """Ask Adzuna for jobs for ONE role and return them as a list."""
    url = "https://api.adzuna.com/v1/api/jobs/in/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": JOBS_PER_ROLE,
        "what": role,
        "max_days_old": MAX_DAYS_OLD,
        "content-type": "application/json",
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("results", [])


def fetch_jobs():
    """Search every role, combine the results, and remove duplicates."""
    all_jobs = []
    seen_links = set()
    for role in ROLES:
        for job in fetch_jobs_for_role(role):
            link = job.get("redirect_url")
            if link in seen_links:
                continue
            seen_links.add(link)
            all_jobs.append(job)
    return all_jobs


def build_email_text(jobs):
    """Turn the list of jobs into a readable email."""
    if not jobs:
        return "No new jobs found today."

    roles_text = ", ".join(ROLES)
    lines = [f"{len(jobs)} jobs found today for: {roles_text}", ""]
    for number, job in enumerate(jobs, start=1):
        company = job.get("company", {}).get("display_name", "Unknown company")
        title = job.get("title", "No title")
        link = job.get("redirect_url", "No link")

        lines.append(f"{number}. {company}")
        lines.append(f"   Job: {title}")
        lines.append("   HR contact: coming soon")
        lines.append(f"   Apply here: {link}")
        lines.append("")
    return "\n".join(lines)


def send_email(body):
    """Send the email from your Gmail to your own Gmail."""
    message = EmailMessage()
    message["Subject"] = "Your daily job list"
    message["From"] = GMAIL_ADDRESS
    message["To"] = GMAIL_ADDRESS
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(message)


if __name__ == "__main__":
    jobs = fetch_jobs()
    email_text = build_email_text(jobs)
    send_email(email_text)
    print(f"Email sent with {len(jobs)} jobs.")
