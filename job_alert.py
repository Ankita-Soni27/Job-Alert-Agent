import json
import os
import re
import smtplib
from email.message import EmailMessage
from itertools import zip_longest

import requests

# --- Your settings (change these anytime) ---
DEFAULT_ROLES = "business analyst fresher, data analyst fresher"
SEARCH_TEXT = os.environ.get("JOB_ROLE") or DEFAULT_ROLES  # read from GitHub setting JOB_ROLE
ROLES = [role.strip() for role in SEARCH_TEXT.split(",") if role.strip()]  # separate roles with commas
MAX_DAYS_OLD = 3           # only jobs posted in the last 3 days
FETCH_PER_ROLE = 50        # how many jobs to ask Adzuna for, for each role
JOBS_PER_DAY = 15          # how many NEW jobs to email each day (each one uses up to 1 Serper search)
DESCRIPTION_LENGTH = 350   # how many characters of the job description to show
SEEN_FILE = "seen_companies.json"   # remembers jobs already sent (just a file name), so they don't repeat

# --- Secrets (GitHub gives these to the script, they are not written here) ---
ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

HR_WORDS = ["hr", "human resources", "recruit", "talent", "hiring", "people"]
COMPANY_SUFFIXES = ["private limited", "pvt ltd", "pvt. ltd.", "limited", "ltd", "llp", "inc", "india", "technologies", "solutions"]


def load_seen():
    """Read the list of jobs we already sent in earlier days."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as file:
            return set(json.load(file))
    return set()


def save_seen(seen):
    """Save the list of jobs we have sent so far."""
    with open(SEEN_FILE, "w") as file:
        json.dump(sorted(seen), file, indent=2)


def job_key(job):
    """A unique ID for a job, used to remember it."""
    return str(job.get("id") or job.get("redirect_url"))


def fetch_jobs_for_role(role):
    """Ask Adzuna for jobs for ONE role and return them as a list."""
    url = "https://api.adzuna.com/v1/api/jobs/in/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": FETCH_PER_ROLE,
        "what": role,
        "max_days_old": MAX_DAYS_OLD,
        "content-type": "application/json",
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("results", [])


def fetch_jobs():
    """Search every role and mix the results (one job of each role in turn), removing duplicates."""
    results_per_role = [fetch_jobs_for_role(role) for role in ROLES]
    all_jobs = []
    seen_keys = set()
    for group in zip_longest(*results_per_role):   # takes 1st of role A, 1st of role B, 2nd of A, 2nd of B...
        for job in group:
            if job is None:
                continue
            key = job_key(job)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_jobs.append(job)
    return all_jobs


def pick_new_jobs(jobs, seen):
    """Keep only jobs we have not sent before, up to JOBS_PER_DAY."""
    new_jobs = [job for job in jobs if job_key(job) not in seen]
    return new_jobs[:JOBS_PER_DAY]


def short_company_name(company):
    """Cut a company name down to its main part, e.g. 'Infosys Limited' -> 'infosys'."""
    name = company.lower()
    for suffix in COMPANY_SUFFIXES:
        name = name.replace(suffix, " ")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(name.split())


def find_hr_contact(company):
    """Google for an HR profile and keep it ONLY if the company name and an HR word both appear in it."""
    if not SERPER_API_KEY:
        return None
    core_name = short_company_name(company) or company.lower()
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f'"{company}" HR recruiter talent acquisition site:linkedin.com/in', "num": 10},
            timeout=30,
        )
        response.raise_for_status()
        for result in response.json().get("organic", []):
            link = result.get("link", "")
            if "linkedin.com/in/" not in link:
                continue
            text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
            text = " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())
            if core_name in text and any(word in text for word in HR_WORDS):
                name = result.get("title", "LinkedIn profile").replace("| LinkedIn", "").strip(" -|")
                return {"name": name, "link": link}
    except Exception as error:
        print(f"HR search failed for {company}: {error}")
    return None


def clean_description(text):
    """Make the job description short and tidy."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_LENGTH:
        text = text[:DESCRIPTION_LENGTH].rsplit(" ", 1)[0] + "..."
    return text or "No description available."


def build_email_text(jobs):
    """Turn the new jobs into a readable email."""
    if not jobs:
        return "No new jobs today. Everything from your search was already sent earlier."

    lines = [f"{len(jobs)} new jobs today for: {', '.join(ROLES)}", ""]
    hr_cache = {}  # so the same company is searched only once
    for number, job in enumerate(jobs, start=1):
        company = job.get("company", {}).get("display_name", "Unknown company")
        if company not in hr_cache:
            hr_cache[company] = find_hr_contact(company)
        hr = hr_cache[company]

        lines.append(f"{number}. {company}")
        lines.append(f"Job: {job.get('title', 'No title')}")
        lines.append(f"About: {clean_description(job.get('description'))}")
        if hr:
            lines.append(f"Possible HR: {hr['name']} - {hr['link']}")
        else:
            lines.append("Possible HR: not found")
        lines.append(f"Apply: {job.get('redirect_url', 'No link')}")
        lines.append("")
        lines.append("-----")
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
    new_jobs = pick_new_jobs(jobs, seen)
    email_text = build_email_text(new_jobs)
    send_email(email_text)

    # Only after the email is sent, remember these jobs so they don't repeat
    seen.update(job_key(job) for job in new_jobs)
    save_seen(seen)
    print(f"Email sent with {len(new_jobs)} new jobs.")
