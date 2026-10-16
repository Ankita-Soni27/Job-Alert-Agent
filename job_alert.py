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
JOBS_PER_DAY = 15          # how many NEW jobs to email each day
ADZUNA_SHARE = 0.5         # half of the jobs from Adzuna, the other half from JSearch
DESCRIPTION_LENGTH = 350   # how many characters of the job description to show
SEEN_FILE = "seen_companies.json"   # remembers jobs already sent (just a file name), so they don't repeat

# --- Secrets (GitHub gives these to the script, they are not written here) ---
ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_APP_KEY = os.environ["ADZUNA_APP_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")

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


# ---------- Source 1: Adzuna ----------

def fetch_adzuna_for_role(role):
    """Ask Adzuna for jobs for ONE role and return them in our common format."""
    response = requests.get(
        "https://api.adzuna.com/v1/api/jobs/in/search/1",
        params={
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "results_per_page": FETCH_PER_ROLE,
            "what": role,
            "max_days_old": MAX_DAYS_OLD,
            "content-type": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    jobs = []
    for item in response.json().get("results", []):
        jobs.append({
            "key": "adzuna-" + str(item.get("id") or item.get("redirect_url")),
            "company": item.get("company", {}).get("display_name", "Unknown company"),
            "title": item.get("title", "No title"),
            "description": item.get("description", ""),
            "link": item.get("redirect_url", "No link"),
        })
    return jobs


# ---------- Source 2: JSearch (through RapidAPI) ----------

def fetch_jsearch_for_role(role):
    """Ask JSearch for jobs for ONE role (uses 1 of your 200 free monthly requests)."""
    response = requests.get(
        "https://jsearch.p.rapidapi.com/search",
        headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
        params={"query": f"{role} in India", "page": "1", "num_pages": "1", "country": "in", "date_posted": "3days"},
        timeout=60,
    )
    response.raise_for_status()
    jobs = []
    for item in response.json().get("data", []):
        jobs.append({
            "key": "jsearch-" + str(item.get("job_id")),
            "company": item.get("employer_name") or "Unknown company",
            "title": item.get("job_title") or "No title",
            "description": item.get("job_description") or "",
            "link": item.get("job_apply_link") or item.get("job_google_link") or "No link",
        })
    return jobs


def fetch_source(fetch_function, source_name):
    """Fetch every role from one source and mix the roles. If the source fails, return an empty list."""
    try:
        per_role = [fetch_function(role) for role in ROLES]
    except Exception as error:
        print(f"{source_name} failed, skipping it: {error}")
        return []
    mixed = []
    for group in zip_longest(*per_role):   # 1st of role A, 1st of role B, 2nd of A, 2nd of B...
        mixed.extend(job for job in group if job is not None)
    return mixed


def pick_new_jobs(adzuna_jobs, jsearch_jobs, seen):
    """Pick about half from each source, skipping jobs already sent and duplicates between the sources."""
    chosen = []
    used_keys = set()
    used_titles = set()

    def take(jobs, how_many):
        taken = 0
        for job in jobs:
            if taken >= how_many:
                break
            title_key = (job["company"].lower(), job["title"].lower())
            if job["key"] in seen or job["key"] in used_keys or title_key in used_titles:
                continue
            used_keys.add(job["key"])
            used_titles.add(title_key)
            chosen.append(job)
            taken += 1

    adzuna_target = round(JOBS_PER_DAY * ADZUNA_SHARE)
    take(adzuna_jobs, adzuna_target)
    take(jsearch_jobs, JOBS_PER_DAY - len(chosen))
    # if JSearch had fewer jobs than its half, fill the rest from Adzuna
    take(adzuna_jobs, JOBS_PER_DAY - len(chosen))
    return chosen


# ---------- HR search ----------

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


# ---------- Email ----------

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
        company = job["company"]
        if company not in hr_cache:
            hr_cache[company] = find_hr_contact(company)
        hr = hr_cache[company]

        lines.append(f"{number}. {company}")
        lines.append(f"Job: {job['title']}")
        lines.append(f"About: {clean_description(job['description'])}")
        if hr:
            lines.append(f"Possible HR: {hr['name']} - {hr['link']}")
        else:
            lines.append("Possible HR: not found")
        lines.append(f"Apply: {job['link']}")
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
    adzuna_jobs = fetch_source(fetch_adzuna_for_role, "Adzuna")
    jsearch_jobs = fetch_source(fetch_jsearch_for_role, "JSearch") if RAPIDAPI_KEY else []
    new_jobs = pick_new_jobs(adzuna_jobs, jsearch_jobs, seen)
    email_text = build_email_text(new_jobs)
    send_email(email_text)

    # Only after the email is sent, remember these jobs so they don't repeat
    seen.update(job["key"] for job in new_jobs)
    save_seen(seen)
    print(f"Email sent with {len(new_jobs)} new jobs.")
