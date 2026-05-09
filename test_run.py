import csv
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["indeed", "linkedin", "zip_recruiter", "google"],
    search_term="software engineer",
    location="amman, jordan",
    results_wanted=5,
    hours_old=72,
    country="Jordan",
    country_indeed="saudi arabia",
)
print(f"Found {len(jobs)} jobs")
print(jobs.head())
