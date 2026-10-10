import json
import os
import smtplib
from email.message import EmailMessage

import requests

# --- Your settings (change these anytime) ---
DEFAULT_ROLES = "business analyst fresher, data analyst fresher"
SEARCH_TEXT = os.environ.get("JOB_ROLE") or DEFAULT_ROLES  # read from GitHub setting JOB_ROLE
ROLES = [role.strip() for role in SEARCH_TEXT.split(",") if role.strip()]  # separate roles with commas
MAX_DAYS_OLD = 3           # only jobs posted in the last 3 days
JOBS_PER_ROLE = 50         # how many jobs to fetch for each role
COMPANIES_PER_DAY = 30     # how many NEW companies to send each day (each one uses 1 Serper search)
SEEN_FILE = "seen_companies.json"   # remembers companies already sent, so they don't repeat

# --- Secrets (GitHub gives these to the script, they are not written here) ---
ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")


def load_seen():
    """Read the list of companies we already sent in earlier days."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as file:
            return set(json.load(file))
    return set()


def save_seen(seen):
    """Save the list of companies we have sent so far."""
    with open(SEEN_FILE, "w") as file:
        json.dump(sorted(seen), file, indent=2)


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
    """Search every role, combine the results, and remove duplicate jobs."""
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


def pick_new_companies(jobs, seen):
    """Group jobs by company and keep only companies we have not sent before."""
    companies = {}  # company name -> list of its jobs
    for job in jobs:
        name = job.get("company", {}).get("display_name")
        if not name or name.lower() in seen:
            continue
        companies.setdefault(name, []).append(job)
    # keep only the first COMPANIES_PER_DAY companies
    return dict(list(companies.items())[:COMPANIES_PER_DAY])


def find_hr_contact(company):
    """Google 'company HR recruiter LinkedIn' through Serper and return the first profile found."""
    if not SERPER_API_KEY:
        return None
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f"{company} HR recruiter talent acquisition site:linkedin.com/in", "num": 5},
            timeout=30,
        )
        response.raise_for_status()
        for result in response.json().get("organic", []):
            link = result.get("link", "")
            if "linkedin.com/in/" in link:
                return {"name": result.get("title", "LinkedIn profile"), "link": link}
    except Exception as error:
        print(f"HR search failed for {company}: {error}")
    return None


def build_email_text(companies):
    """Turn the new companies into a readable email."""
    if not companies:
        return "No new companies today. Everything from your search was already sent earlier."

    roles_text = ", ".join(ROLES)
    lines = [f"{len(companies)} new companies today for: {roles_text}", ""]
    for number, (company, company_jobs) in enumerate(companies.items(), start=1):
        first = company_jobs[0]
        hr = find_hr_contact(company)

        lines.append(f"{number}. {company}")
        lines.append(f"   Job: {first.get('title', 'No title')}")
        if hr:
            lines.append(f"   Possible HR contact: {hr['name']}")
            lines.append(f"   {hr['link']}")
        else:
            lines.append("   Possible HR contact: not found")
        lines.append(f"   Apply here: {first.get('redirect_url', 'No link')}")
        for other in company_jobs[1:3]:  # up to 2 more openings at the same company
            lines.append(f"   Also hiring: {other.get('title', 'No title')} - {other.get('redirect_url', '')}")
        lines.append("")
    lines.append("Note: HR contacts come from a Google search and may be wrong. "
                 "Check the profile before messaging anyone.")
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
    seen = load_seen()
    jobs = fetch_jobs()
    new_companies = pick_new_companies(jobs, seen)
    email_text = build_email_text(new_companies)
    send_email(email_text)

    # Only after the email is sent, remember these companies so they don't repeat
    seen.update(name.lower() for name in new_companies)
    save_seen(seen)
    print(f"Email sent with {len(new_companies)} new companies.")
