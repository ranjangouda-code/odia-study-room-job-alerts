import os
import json
import re
import html
import requests

from urllib.parse import urlparse
from google.oauth2 import service_account
from google.auth.transport.requests import Request


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BLOGGER_FEED = (
    "https://www.odiastudyroom.in/feeds/posts/default"
    "?alt=json&max-results=50&orderby=published"
)

FIREBASE_PROJECT_ID = "odia-study-room-job-updates"

FCM_URL = (
    f"https://fcm.googleapis.com/v1/projects/"
    f"{FIREBASE_PROJECT_ID}/messages:send"
)

STATE_FILE = "seen_posts.json"

ALLOWED_CATEGORIES = {
    "Latest Jobs",
    "Admit Card",
    "Exam Date",
    "Results",
    "Upcoming Jobs"
}


# --------------------------------------------------
# TEXT CLEANING
# --------------------------------------------------

def clean_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(value):
    value = clean_html(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


# --------------------------------------------------
# CATEGORY DETECTION
# --------------------------------------------------

CATEGORY_RULES = [
    (
        "Admit Card",
        [
            "admit card",
            "admit cards",
            "hall ticket",
            "call letter"
        ]
    ),
    (
        "Exam Date",
        [
            "exam date",
            "exam dates",
            "exam schedule",
            "exam timetable",
            "examination date"
        ]
    ),
    (
        "Results",
        [
            "result",
            "results",
            "merit list",
            "score card",
            "scorecard"
        ]
    ),
    (
        "Upcoming Jobs",
        [
            "upcoming job",
            "upcoming jobs",
            "upcoming recruitment",
            "upcoming vacancy",
            "upcoming vacancies"
        ]
    ),
    (
        "Latest Jobs",
        [
            "latest job",
            "latest jobs",
            "recruitment",
            "government jobs",
            "govt jobs",
            "job notification",
            "job notifications",
            "vacancy",
            "vacancies"
        ]
    )
]


def detect_category(text):
    normalized = normalize_text(text)

    for category, keywords in CATEGORY_RULES:
        for keyword in keywords:
            if keyword in normalized:
                return category

    return None


def get_category(entry):

    labels = []

    for item in entry.get("category", []):
        term = item.get("term", "")

        if term:
            labels.append(term.strip())

    # First check Blogger labels.
    for label in labels:
        category = detect_category(label)

        if category:
            return category, labels, "label"

    # If no label matches, check the post title.
    title = entry.get("title", {}).get("$t", "")
    category = detect_category(title)

    if category:
        return category, labels, "title"

    return None, labels, "unmatched"


# --------------------------------------------------
# NOTIFICATION HISTORY
# --------------------------------------------------

def load_seen_posts():

    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            return set(data)

        print("WARNING: Invalid notification history format.")
        return set()

    except (json.JSONDecodeError, OSError) as error:
        print("WARNING: Could not read notification history:", error)
        return set()


def save_seen_posts(seen):

    temporary_file = STATE_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)

    os.replace(temporary_file, STATE_FILE)


# --------------------------------------------------
# FETCH BLOGGER POSTS
# --------------------------------------------------

def get_posts():

    print("Fetching Blogger feed...")

    response = requests.get(
        BLOGGER_FEED,
        headers={"User-Agent": "OdiaStudyRoomJobAlerts/1.0"},
        timeout=30
    )

    response.raise_for_status()

    feed = response.json().get("feed", {})
    entries = feed.get("entry", [])

    posts = []

    print("Total Blogger entries:", len(entries))

    for entry in entries:

        title = clean_html(
            entry.get("title", {}).get("$t", "")
        )

        category, labels, matched_by = get_category(entry)

        print("\n--------------------------------")
        print("Post:", title)
        print("Blogger labels:", labels)

        if category not in ALLOWED_CATEGORIES:
            print("SKIPPED: No eligible category matched.")
            continue

        print("Category:", category)
        print("Matched using:", matched_by)

        links = entry.get("link", [])

        url = next(
            (
                link.get("href")
                for link in links
                if link.get("rel") == "alternate"
                and link.get("href")
            ),
            None
        )

        if not title or not url:
            print("SKIPPED: Missing title or article URL.")
            continue

        parsed_url = urlparse(url)

        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "www.odiastudyroom.in"
        ):
            print("SKIPPED: Invalid article URL:", url)
            continue

        published = entry.get("published", {}).get("$t", "")

        content = (
            entry.get("summary", {}).get("$t")
            or entry.get("content", {}).get("$t", "")
        )

        summary = clean_html(content)[:300]

        posts.append({
            "id": url,
            "title": title,
            "url": url,
            "date": published,
            "category": category,
            "summary": summary
        })

        print("ACCEPTED:", title)

    print("\n================================")
    print("Eligible posts found:", len(posts))
    print("================================")

    return posts


# --------------------------------------------------
# FIREBASE ACCESS TOKEN
# --------------------------------------------------

def get_access_token():

    secret = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not secret:
        raise RuntimeError(
            "Missing FIREBASE_SERVICE_ACCOUNT_JSON GitHub secret"
        )

    service_account_info = json.loads(secret)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/firebase.messaging"
        ]
    )

    credentials.refresh(Request())

    return credentials.token


# --------------------------------------------------
# SEND FIREBASE NOTIFICATION
# --------------------------------------------------

def send_notification(post, access_token):

    summary = (
        post["summary"]
        or "Open the app to read the full notification."
    )

    body = summary[:180]

    message = {
        "message": {
            "topic": "all_job_alerts",

            "data": {
                "title": post["title"][:200],
                "body": body,
                "url": post["url"],
                "date": post["date"],
                "category": post["category"],
                "summary": summary
            },

            "android": {
                "priority": "HIGH"
            }
        }
    }

    response = requests.post(
        FCM_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json=message,
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"FCM error {response.status_code}: {response.text}"
        )

    print("Firebase notification sent:", post["title"])
    print("Firebase response:", response.text)


# --------------------------------------------------
# MAIN MONITOR
# --------------------------------------------------

def main():

    print("Checking Blogger for new job notifications...")

    posts = get_posts()

    seen = load_seen_posts()

    # First run: save existing eligible posts without
    # sending old notifications.
    if seen is None:

        save_seen_posts({
            post["id"] for post in posts
        })

        print(
            "Initial setup completed.",
            len(posts),
            "existing eligible posts saved."
        )

        return

    new_posts = [
        post for post in reversed(posts)
        if post["id"] not in seen
    ]

    if not new_posts:
        print("No new job notifications.")
        return

    print("New posts detected:", len(new_posts))

    access_token = get_access_token()

    sent_count = 0

    for post in new_posts:

        # Send first. Only mark a post as seen after
        # Firebase accepts the message.
        send_notification(post, access_token)

        seen.add(post["id"])
        save_seen_posts(seen)

        sent_count += 1

    print(
        "Completed:",
        sent_count,
        "new notifications sent successfully."
    )


if __name__ == "__main__":
    main()
