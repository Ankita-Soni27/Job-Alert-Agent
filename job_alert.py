import json
import os
import re
import smtplib
from email.message import EmailMessage
from urllib.parse import quote, urlparse

import requests

# --- Your settings (change these anytime) ---
DEFAULT_ROLES = "business analyst fresher, data analyst fresher"
SEARCH_TEXT = os.environ.get("JOB_ROLE") or DEFAULT_ROLES  # read from GitHub setting JOB_ROLE
ROLES = [role.strip() for role in SEARCH_TEXT.split(",") if role.strip()]  # separate roles with commas
MAX_DAYS_OLD = 3           # only jobs posted in the last 3 days
JOBS_PER_ROLE = 50         # how many jobs to fetch for each role
COMPANIES_PER_DAY = 30     # how many NEW companies to send each day
COMPANY_CHECKS_PER_DAY = 3 # TRIAL: only check if the company looks genuine for this many companies (1 Serper search each). Raise later, e.g. to 30
HR_LOOKUPS_PER_DAY = 3     # TRIAL: only search HR for this many companies (each uses 1 Serper search). Raise later, e.g. to 30
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


HR_WORDS = ["hr", "human resources", "recruit", "talent", "hiring", "people"]
COMPANY_SUFFIXES = ["private limited", "pvt ltd", "pvt. ltd.", "limited", "ltd", "llp", "inc", "india", "technologies", "solutions"]


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
            text = re.sub(r"[^a-z0-9 ]", " ", text)
            text = " ".join(text.split())
            has_company = core_name in text
            has_hr_word = any(word in text for word in HR_WORDS)
            if has_company and has_hr_word:
                return {"name": result.get("title", "LinkedIn profile"), "link": link}
    except Exception as error:
        print(f"HR search failed for {company}: {error}")
    return None


NOT_A_COMPANY_WEBSITE = [
    "linkedin.", "facebook.", "instagram.", "twitter.", "x.com", "youtube.", "wikipedia.", "quora.", "reddit.",
    "ambitionbox.", "glassdoor.", "indeed.", "naukri.", "justdial.", "zaubacorp.", "tofler.", "crunchbase.",
    "adzuna.", "instahyre.", "foundit.", "shine.", "monster.", "sulekha.", "indiamart.", "mca.gov.in",
]


def check_company(company):
    """One Serper search to see if the company has a website, LinkedIn page, reviews and registration info."""
    if not SERPER_API_KEY:
        return None
    core_name = short_company_name(company) or company.lower()
    squashed = core_name.replace(" ", "")
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f"{company} company India", "num": 10},
            timeout=30,
        )
        response.raise_for_status()
        results = response.json().get("organic", [])
    except Exception as error:
        print(f"Company check failed for {company}: {error}")
        return None

    def matches(result):
        link = result.get("link", "").lower()
        text = f"{result.get('title', '')} {link}".lower()
        text = " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())
        return core_name in text or squashed in link.replace("-", "").replace(".", "")

    found = {"website": None, "linkedin": None, "reviews": None, "registration": None}
    for result in results:
        link = result.get("link", "")
        low = link.lower()
        if not matches(result):
            continue
        if "linkedin.com/company/" in low and not found["linkedin"]:
            found["linkedin"] = link
        elif ("ambitionbox." in low or "glassdoor." in low) and not found["reviews"]:
            found["reviews"] = link
        elif ("zaubacorp." in low or "tofler." in low) and not found["registration"]:
            found["registration"] = link
        elif not any(bad in low for bad in NOT_A_COMPANY_WEBSITE) and not found["website"]:
            found["website"] = link
    return found


def company_check_lines(company, checked):
    """Turn the company check into short email lines."""
    if not checked:
        return ["   Company check: not checked today"]
    found = check_company(company)
    if found is None:
        return ["   Company check: could not check (search failed)"]

    score = sum(1 for value in found.values() if value)
    if score >= 3:
        verdict = "Looks established"
    elif score == 2:
        verdict = "Limited info - check carefully"
    else:
        verdict = "WARNING - very little info found, be careful"

    labels = [("website", "Website"), ("linkedin", "LinkedIn page"),
              ("reviews", "Reviews"), ("registration", "Registration info")]
    lines = [f"   Company check: {score}/4 signals - {verdict}"]
    for key, label in labels:
        lines.append(f"     {label}: {found[key] or 'not found'}")
    return lines


def linkedin_search_link(company):
    """A LinkedIn people-search link the person can click to see current HR staff of the company."""
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} HR recruiter")


def build_email_text(companies):
    """Turn the new companies into a readable email."""
    if not companies:
        return "No new companies today. Everything from your search was already sent earlier."

    roles_text = ", ".join(ROLES)
    lines = [f"{len(companies)} new companies today for: {roles_text}", ""]
    for number, (company, company_jobs) in enumerate(companies.items(), start=1):
        first = company_jobs[0]
        searched = number <= HR_LOOKUPS_PER_DAY
        hr = find_hr_contact(company) if searched else None

        lines.append(f"{number}. {company}")
        lines.append(f"   Job: {first.get('title', 'No title')}")
        if hr:
            lines.append(f"   Possible HR contact: {hr['name']}")
            lines.append(f"   {hr['link']}")
        elif searched:
            lines.append("   Possible HR contact: not found")
        else:
            lines.append("   Possible HR contact: not searched today")
        lines.append(f"   Find HR on LinkedIn: {linkedin_search_link(company)}")
        lines.extend(company_check_lines(company, number <= COMPANY_CHECKS_PER_DAY))
        lines.append(f"   Apply here: {first.get('redirect_url', 'No link')}")
        for other in company_jobs[1:3]:  # up to 2 more openings at the same company
            lines.append(f"   Also hiring: {other.get('title', 'No title')} - {other.get('redirect_url', '')}")
        lines.append("")
    lines.append("Note: HR contacts come from a Google search and may be wrong. "
                 "Check the profile before messaging anyone. "
                 "The company check is only a guide, not proof that a company is genuine.")
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
