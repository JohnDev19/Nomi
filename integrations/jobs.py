import requests

USER_AGENT = "Mozilla/5.0 (personal-assistant-bot)"
JUNIOR_KEYWORDS = ("junior", "jr.", "jr ", "entry level", "entry-level", "graduate")


def fetch_junior_dev_jobs(limit=5):
    """Pulls recent junior/entry-level dev postings from RemoteOK's public API (no key needed)."""
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        listings = resp.json()
    except (requests.RequestException, ValueError):
        return []

    # the feed's first item is just metadata, not a real posting, so filter that out
    listings = [job for job in listings if isinstance(job, dict) and job.get("id")]

    results = []
    for job in listings:
        position = (job.get("position") or "").lower()
        tags = " ".join(job.get("tags", [])).lower()

        if any(kw in position or kw in tags for kw in JUNIOR_KEYWORDS):
            results.append({
                "title": job.get("position", "Unknown role"),
                "company": job.get("company", "Unknown company"),
                "url": job.get("url", ""),
            })

        if len(results) >= limit:
            break

    return results


def format_jobs_message(jobs):
    if not jobs:
        return "Wala akong nahanap na junior dev openings ngayon, try ko ulit next time."

    lines = ["Here are the junior dev openings I found:"]
    for i, job in enumerate(jobs, start=1):
        lines.append(f"{i}. {job['title']} @ {job['company']}\n{job['url']}")

    return "\n\n".join(lines)
