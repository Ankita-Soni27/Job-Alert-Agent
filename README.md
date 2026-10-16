# Job Alert Agent

A Python automation that finds fresh Business Analyst and Data Analyst jobs every day and emails me a clean list, so I don't have to check job sites by hand.

## What it does

Every day it:

1. Fetches recent jobs from two sources: **Adzuna** and **JSearch** (which collects jobs from LinkedIn, Indeed, Glassdoor and others via Google for Jobs).
2. Keeps only jobs that match my roles, my target cities, and 0-3 years of experience.
3. Picks 15 new jobs, about half from each source, and never repeats a job it already sent.
4. Searches Google (through Serper) for a **possible HR / recruiter contact** at each company.
5. Emails the list to my Gmail.

It runs on its own using GitHub Actions, so my laptop doesn't need to be on.

## What the email looks like

```
15 new jobs today for: associate data analyst, associate business analyst, data analyst, business analyst
Cities: Delhi, Noida, Gurgaon, Bangalore, Hyderabad

1. Example Company Pvt Ltd
Job: Business Analyst
Location: Noida, Uttar Pradesh
About: Short preview of the job description...
Possible HR: Name - Talent Acquisition - Example Company (LinkedIn link)
Apply: https://...
```

## Settings

All settings are at the top of `job_alert.py`:

| Setting | What it controls |
|---|---|
| `CITIES` | Cities to search (Delhi, Noida, Gurgaon, Bangalore, Hyderabad) |
| `JOBS_PER_DAY` | How many new jobs to email each day (15) |
| `ADZUNA_SHARE` | Share of jobs from Adzuna; the rest come from JSearch (0.5) |
| `MAX_EXPERIENCE_YEARS` | Skip jobs that ask for more experience than this (3) |
| `MAX_DAYS_OLD` | Only jobs posted in the last N days (3) |

The job roles can be changed without editing code, using a GitHub repository variable called `JOB_ROLE` (roles separated by commas).

## How it is built

- **Language:** Python (`requests`, `smtplib`)
- **Job data:** Adzuna API, JSearch API (RapidAPI)
- **HR search:** Serper (Google Search API)
- **Scheduling:** GitHub Actions (`.github/workflows/daily.yml`)
- **Memory:** a small JSON file, committed back to the repo by the workflow, remembers which jobs were already sent
- **Secrets:** API keys and the Gmail App Password are stored as GitHub Secrets, never in the code

## Set it up yourself

1. Fork or copy this repo.
2. Get free keys from Adzuna, RapidAPI (JSearch) and Serper, and create a Gmail App Password.
3. In the repo, go to **Settings → Secrets and variables → Actions** and add these secrets:
   `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `RAPIDAPI_KEY`, `SERPER_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`
4. (Optional) Add a variable `JOB_ROLE` with the roles you want.
5. Go to the **Actions** tab, open **Daily Job Alert** and click **Run workflow** to test it.

## Things to know

- **HR contacts are only "possible".** They come from a Google search, so the person can be wrong or may have left the company. Always check the profile before messaging anyone.
- **The experience filter is a best guess.** Many job posts don't mention experience, so a few jobs outside 0-3 years may slip through.
- **Adzuna links** go through Adzuna's own page first, and some expired jobs may not open.
- **Free plan limits:** JSearch allows 200 requests a month, so its searches rotate through the cities day by day.
- **Timing:** GitHub starts scheduled jobs when it has free capacity, so the email can arrive a few hours after the scheduled time.

## What I learned

Working with REST APIs, handling API failures without breaking the whole run, removing duplicate data, storing secrets safely, and automating a task with GitHub Actions.
