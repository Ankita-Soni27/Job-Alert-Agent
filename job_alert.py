import json
import os
import re
import smtplib
import time
from datetime import date
from email.message import EmailMessage
from itertools import zip_longest

import requests

# --- Your settings (change these anytime) ---
DEFAULT_ROLES = "associate data analyst, associate business analyst, data analyst, business analyst"
SEARCH_TEXT = os.environ.get("JOB_ROLE") or DEFAULT_ROLES  # read from GitHub setting JOB_ROLE
ROLES = [role.strip() for role in SEARCH_TEXT.split(",") if role.strip()]  # separate roles with commas
# City name used for searching -> words that count as a match in a job's location
CITIES = {
    "Delhi": ["delhi"],
    "Noida": ["noida"],
    "Gurgaon": ["gurgaon", "gurugram"],
    "Bangalore": ["bangalore", "bengaluru", "bangaluru"],
    "Hyderabad": ["hyderabad", "secunderabad"],
}
JSEARCH_REQUESTS_PER_RUN = 4   # JSearch free plan = 200 requests a month, so we rotate through cities day by day
MAX_DAYS_OLD = 3           # only jobs posted in the last 3 days
MAX_EXPERIENCE_YEARS = 3   # skip jobs that ask for more than this many years of experience
FETCH_PER_SEARCH = 50      # how many jobs to ask Adzuna for, for each role + city search
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


def base_roles():
    """'associate data analyst' and 'data analyst' are searched together as 'data analyst'."""
    roles = []
    for role in ROLES:
        base = re.sub(r"^associate\s+", "", role, flags=re.I).strip()
        if base and base not in roles:
            roles.append(base)
    return roles


def in_target_city(job):
    """True if the job's location is one of the cities you asked for."""
    location = (job.get("location") or "").lower()
    return any(word in location for words in CITIES.values() for word in words)


# ---------- Source 1: Adzuna ----------

def fetch_adzuna(role, city):
    """Ask Adzuna for jobs for ONE role in ONE city and return them in our common format."""
    time.sleep(0.5)   # be gentle with Adzuna's request limit
    response = requests.get(
        "https://api.adzuna.com/v1/api/jobs/in/search/1",
        params={
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "results_per_page": FETCH_PER_SEARCH,
            "what": role,
            "where": city,
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
            "location": item.get("location", {}).get("display_name", ""),
        })
    return jobs


# ---------- Source 2: JSearch (through RapidAPI) ----------

def fetch_jsearch(role, city):
    """Ask JSearch for jobs for ONE role in ONE city (uses 1 of your 200 free monthly requests)."""
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    params = {"query": f"{role} in {city}", "country": "in", "date_posted": "3days"}

    # JSearch has a newer address (search-v2) and an older one (search). Try the new one first.
    payload = None
    for endpoint in ("search-v2", "search"):
        response = requests.get(f"https://jsearch.p.rapidapi.com/{endpoint}",
                                headers=headers, params=params, timeout=60)
        if response.status_code == 404:
            continue   # this address does not exist, try the next one
        response.raise_for_status()
        payload = response.json()
        break
    if payload is None:
        raise RuntimeError("JSearch address not found (404) on both search-v2 and search")

    items = payload.get("data", [])
    if isinstance(items, dict):   # some versions wrap the list inside another box
        items = items.get("jobs") or items.get("results") or []

    jobs = []
    for item in items:
        location = item.get("job_location") or ", ".join(
            part for part in (item.get("job_city"), item.get("job_state")) if part)
        jobs.append({
            "key": "jsearch-" + str(item.get("job_id")),
            "company": item.get("employer_name") or "Unknown company",
            "title": item.get("job_title") or "No title",
            "description": item.get("job_description") or "",
            "link": item.get("job_apply_link") or item.get("job_google_link") or "No link",
            "experience_years": item.get("required_experience_years"),
            "location": location,
        })
    return jobs


def todays_jsearch_searches():
    """Pick a few role+city searches for today. They rotate daily so every city gets covered."""
    all_searches = [(role, city) for role in base_roles() for city in CITIES]
    start = (date.today().toordinal() * JSEARCH_REQUESTS_PER_RUN) % len(all_searches)
    count = min(JSEARCH_REQUESTS_PER_RUN, len(all_searches))
    return [all_searches[(start + i) % len(all_searches)] for i in range(count)]


PROBLEMS = []   # filled in if a job source fails, and shown at the bottom of the email


def fetch_source(fetch_function, source_name, searches):
    """Run every role+city search for one source and mix the results. If the source fails, return an empty list."""
    try:
        per_search = [fetch_function(role, city) for role, city in searches]
    except Exception as error:
        print(f"{source_name} failed, skipping it: {error}")
        PROBLEMS.append(f"{source_name} did not work today: {error}")
        return []
    mixed = []
    for group in zip_longest(*per_search):   # 1st of search A, 1st of search B, 2nd of A, 2nd of B...
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
            if too_experienced(job) or not in_target_city(job):
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


# ---------- Experience filter (keeps 0-3 years jobs) ----------

SENIOR_TITLE = re.compile(
    r"\b(senior|sr|lead|manager|head|principal|director|architect|vp|staff|chief)\b|\bl[3-9]\b", re.I)
YEARS_RANGE = re.compile(r"(\d{1,2})\s*(?:-|to|\u2013)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
YEARS_SINGLE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)


def too_experienced(job):
    """True if the job looks senior or asks for more than MAX_EXPERIENCE_YEARS years of experience."""
    if SENIOR_TITLE.search(job["title"]):
        return True
    years = job.get("experience_years")   # JSearch tells us this directly for many jobs
    if isinstance(years, (int, float)) and years > MAX_EXPERIENCE_YEARS:
        return True
    text = re.sub(r"<[^>]+>", " ", job["description"] or "")
    wanted = []
    for pattern in (YEARS_RANGE, YEARS_SINGLE):
        for match in pattern.finditer(text):
            around = text[max(0, match.start() - 40): match.end() + 40].lower()
            if "experience" in around or "exp" in around:
                wanted.append(int(match.group(1)))   # the lower number, e.g. 3 in "3-5 years"
    return bool(wanted) and min(wanted) > MAX_EXPERIENCE_YEARS


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

    lines = [f"{len(jobs)} new jobs today for: {', '.join(ROLES)}", f"Cities: {', '.join(CITIES)}", ""]
    hr_cache = {}  # so the same company is searched only once
    for number, job in enumerate(jobs, start=1):
        company = job["company"]
        if company not in hr_cache:
            hr_cache[company] = find_hr_contact(company)
        hr = hr_cache[company]

        lines.append(f"{number}. {company}")
        lines.append(f"Job: {job['title']}")
        lines.append(f"Location: {job.get('location') or 'Not given'}")
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
    for problem in PROBLEMS:
        lines.append(f"Problem: {problem}")
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
    adzuna_searches = [(role, city) for role in base_roles() for city in CITIES]
    adzuna_jobs = fetch_source(fetch_adzuna, "Adzuna", adzuna_searches)
    if RAPIDAPI_KEY:
        jsearch_jobs = fetch_source(fetch_jsearch, "JSearch", todays_jsearch_searches())
    else:
        jsearch_jobs = []
        PROBLEMS.append("JSearch was skipped because the RAPIDAPI_KEY secret was not found.")
    new_jobs = pick_new_jobs(adzuna_jobs, jsearch_jobs, seen)
    email_text = build_email_text(new_jobs)
    send_email(email_text)

    # Only after the email is sent, remember these jobs so they don't repeat
    seen.update(job["key"] for job in new_jobs)
    save_seen(seen)
    print(f"Email sent with {len(new_jobs)} new jobs.")
