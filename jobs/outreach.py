"""Outreach pack for one opening: everything a writing session needs to draft a message to a person.

`python -m jobs outreach <job_id>` writes this as JSON. The message itself is written by
whoever reads the pack (a Claude session with the linkedin-outreach skill, or you); the
pack holds the opening, the profile, the LinkedIn searches that find the right people, and
the shape each message type follows. Nothing here contacts anyone.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from jobs.network import job_posts_search_url, people_search_url

MAX_DESCRIPTION_CHARS = 6000

# Who to look for at the company, per message type, as LinkedIn people-search keywords.
PEOPLE_TO_FIND = {
    "hiring_manager": ["VP Engineering", "Head of Engineering", "CTO", "Director of Engineering"],
    "recruiter": ["Technical Recruiter", "Talent Acquisition", "Recruiter"],
    "referrer": ["Engineering Manager", "Staff Engineer", "Senior Software Engineer"],
}

MESSAGE_TYPES = {
    "recruiter-or-hiring-manager": [
        "greeting",
        "who I am (one sentence, current role)",
        "interest: the role by title, where I saw it, one honest reason it fits",
        "hook: two or three concrete matching facts, at most one number",
        "why this company: one specific thing from the posting or their own pages",
        "the ask: a short conversation, or whether the application reached the right person; mention attachments",
        "close: thank you for your time; Regards, <name>",
    ],
    "referral": [
        "greeting",
        "who I am",
        "why this person: a real reason (their path, a post, shared context); skip if none is known",
        "interest: the opening by title and one honest reason it fits",
        "hook: two or three matching facts, at most one number",
        "the ask: refer me, pass my CV on, or name the right person; one question",
        "close",
    ],
    "networking": [
        "greeting",
        "who I am (one sentence, field)",
        "why this person: a post, shared context, or following their work, and what it made me think",
        "hook: two or three facts about what I do, at most one number",
        "the ask plus what I offer: a 20-minute call about X, and what I can bring",
        "call to action with a choice: this week or next?",
        "close: Thanks, <first name>",
    ],
    "connection-note": [
        "under 300 characters, no ask: who I am and the one real thing that made me click Connect; "
        "if there is nothing real, send the invitation without a note",
    ],
}

INSTRUCTIONS = (
    "Write one message for the chosen type, 90 to 140 words (a connection note under 300 characters), "
    "plain first person, in the language the reader uses. Use only facts from the profile and what the "
    "sender confirms; never invent a compliment or a shared context. One number at most, and only a "
    "confirmed one. No 'I hope this finds you well', no CV bullets, no list of tools. Ask for a "
    "conversation, a referral or a name, never for a job. Never send anything; return the text and "
    "where it goes."
)


def build_pack(row: dict[str, str], profile: str) -> dict:
    company = row.get("company", "")
    title = row.get("title", "")
    return {
        "instructions": INSTRUCTIONS,
        "profile": profile,
        "job": {
            "job_id": row.get("job_id", ""),
            "title": title,
            "company": company,
            "location": row.get("location", ""),
            "workplace": row.get("workplace", ""),
            "url": row.get("url", ""),
            "score": row.get("score", ""),
            "verdict": row.get("verdict", ""),
            "status": row.get("status", ""),
            "strong_matches": row.get("strong_matches", ""),
            "gaps": row.get("gaps", ""),
            "pitch": row.get("pitch", ""),
            "description": (row.get("description") or "")[:MAX_DESCRIPTION_CHARS],
        },
        "find_people": {
            kind: [{"role": role, "url": people_search_url(company, role)} for role in roles]
            for kind, roles in PEOPLE_TO_FIND.items()
        },
        "company_page_search": f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(company)}",
        "job_posts_search": job_posts_search_url(title),
        "message_types": MESSAGE_TYPES,
    }
