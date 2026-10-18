"""Everything Jev is told about how to judge a job against the profile lives here.

Tune the wording freely; the question keys and their choice labels are what the rest of
the code reads (see jobs/scoring/jev.py), so keep those stable.

Jev question types used (OpenRouter decisions API, model typesafe/jev-1.13):
  choice  -> {"type": "choice", "instructions": ..., "criteria": {label: description}}
             answer: {"type": "choice", "choice": label, "confidence": 0..1}
  noul    -> {"type": "noul", "instructions": ...}
             answer: {"type": "noul", "noul": 0..1}   (a probability)

The overall `fit` question is asked as a `noul` and scaled to 0..100. The sources I could
reach (jevhunt.com blog, OpenRouter cookbook) document only `choice` and `noul`; a `score`
type is mentioned by name but its request shape is not published. If you confirm the
`score` shape, change FIT_QUESTION below and `parse_answers` in jev.py will pick up a
numeric `score` / `value` field automatically.
"""

from __future__ import annotations

PROFILE_PREAMBLE = (
    "You are screening job postings for one candidate. The candidate's profile (CV summary, "
    "target roles, constraints and preferences) is in state.profile. The job posting is in "
    "state.job. Judge the job strictly against the profile: the question is whether this "
    "candidate should spend time applying, not whether the job is good in general."
)

FIT_QUESTION = {
    "type": "noul",
    "instructions": (
        "Is this job a strong match for the candidate? Weigh, in this order: role level "
        "(engineering manager / head of engineering versus individual contributor), "
        "workplace and location against the candidate's constraints, required languages, "
        "team type, stack, experience fit, and then team size and scope. Team type: a "
        "customer-facing product team is the candidate's preference and experience; a "
        "backend-only, SRE, infrastructure, data-platform, security or internal-platform "
        "team is a real minus. Stack: follow the posting's own wording. A stack the posting "
        "requires is weighted heavily: a match with the profile's core stack is a strong plus, "
        "a requirement for hands-on depth the profile lacks is a strong minus. A stack listed "
        "as desired or nice-to-have moves the score only a little either way; a "
        "stack-agnostic management role is neutral. Experience fit: the candidate has built "
        "products, teams, processes and systems from scratch many times, so postings about "
        "building or scaling a team, setting up processes, greenfield work or taking over "
        "from an agency get a plus. Modern ways of working named in the posting (remote-first, "
        "asynchronous, autonomy, continuous delivery, AI-assisted engineering) are a plus; "
        "traditional setups (heavy process, long release cycles, delivery or project "
        "management framing, PMO) are a minus; AI-native setups where agents write most code "
        "are a plus. Startup or scale-up stage is a small plus, very early stage neutral, "
        "corporates neutral when the team works in a modern way. Follow the profile's "
        "'Preferences confirmed' section for everything it lists as blocker, minus, neutral "
        "or plus (employment type, relocation, consultancies, languages, title level, travel). "
        "Treat a clear mismatch on level "
        "(an IC role, or a VP/CTO role far above the profile) or an impossible location or "
        "language requirement as a weak match even when the domain fits. Workplace matters "
        "a lot: with everything else equal, a remote role ranks clearly above a hybrid role "
        "with few office days, which ranks above a hybrid role with many office days, which "
        "ranks above a fully on-site role. Follow the profile's own words on how many office "
        "days are acceptable."
    ),
}

VERDICT_QUESTION = {
    "type": "choice",
    "instructions": (
        "Should the candidate apply? Use the profile's constraints as hard filters and the "
        "profile's preferences as soft signals. The core of the decision is level, scope and "
        "workplace. A domain or tech-stack gap (for example infrastructure, data, security, "
        "fintech or a language the candidate has not used) is not a reason to withhold "
        "'apply' for a people-management role: engineering managers move between domains, "
        "and the explanation pass will list the gap. Withhold 'apply' for a domain gap only "
        "when the posting requires deep hands-on expertise in it as a must-have."
    ),
    "criteria": {
        "apply": "Engineering manager, head of engineering or equivalent people-leadership role at the profile's level, remote or hybrid with an office-day count the profile accepts, no language blocker, and no stack the posting requires that the profile lacks. A customer-facing product team, or a stack-agnostic role. Desired-but-missing skills or an unfamiliar business domain do not disqualify.",
        "maybe": "Right level but an important unknown or soft problem: workplace not stated, more office days than the profile prefers but not ruled out, unclear team scope, a backend-only / SRE / infrastructure / platform team, a required stack the profile lacks when the role is otherwise strong, or a traditional delivery-management framing.",
        "skip": "Hard blocker: wrong level (IC, first-time lead, or VP/CTO far above), impossible location, fully on-site where the profile rules it out, a required or everyday language the profile marks as a blocker, or an engagement type the profile rules out (for example contractor or freelance when it wants an employee contract).",
    },
}

SENIORITY_QUESTION = {
    "type": "choice",
    "instructions": (
        "Compare the seniority the job asks for with the candidate's level as described in the profile."
    ),
    "criteria": {
        "below": "The job is junior to the candidate: an individual contributor role, a first-time team lead role, or a lead without people responsibility.",
        "match": "Engineering manager, head of engineering or equivalent people-leadership role at the scope the profile describes.",
        "above": "The job is senior to the candidate: VP of engineering, CTO or director over several managers, beyond what the profile supports.",
    },
}

LOCATION_QUESTION = {
    "type": "choice",
    "instructions": (
        "Can the candidate do this job from where they live, given the profile's location, "
        "relocation and remote constraints?"
    ),
    "criteria": {
        "yes": "Remote from the candidate's country/EU as the profile allows, or hybrid within the candidate's commuting area with an office-day count the profile accepts.",
        "no": "Requires presence in a place the candidate cannot commute to or relocate to, remote only from a country the candidate is not in, or fully on-site when the profile says on-site is not acceptable.",
        "unclear": "The posting does not say where the work happens, whether remote is allowed, or how many office days a hybrid role needs.",
    },
}

LANGUAGE_QUESTION = {
    "type": "choice",
    "instructions": (
        "Does the job require, or use day to day, a language the candidate does not have at "
        "the needed level? Use the profile's language section: it says which languages the "
        "candidate speaks, at what level, and which language situations it treats as a "
        "blocker or a minus. A posting written in a language is a strong sign that language "
        "is the working language."
    ),
    "criteria": {
        "none": "No language requirement beyond what the profile covers at the needed level, and the working language is one the profile lists as fluent.",
        "missing": "A language the profile does not cover, or marks as a blocker, is required, clearly expected, or the everyday working language.",
        "partial": "The working or required language is one the profile lists below the needed level (for example an intermediate level where fluent is expected) and the profile treats that as a minus rather than a blocker.",
    },
}

TEAM_TYPE_QUESTION = {
    "type": "choice",
    "instructions": (
        "What kind of engineering team would the candidate lead, according to the posting?"
    ),
    "criteria": {
        "product": "A customer-facing product: web or mobile apps, marketplaces, e-commerce, SaaS features, a product team working with product managers and designers.",
        "platform_infra": "Backend-only, SRE, infrastructure, cloud, data platform, security, identity, internal developer platform, managed services or operations.",
        "unclear": "The posting does not say what the team builds or for whom.",
    },
}

STACK_FIT_QUESTION = {
    "type": "choice",
    "instructions": (
        "Compare the technologies the posting names with the profile's stack, using the "
        "posting's own wording for required versus desired."
    ),
    "criteria": {
        "required_match": "The posting requires or expects hands-on experience in a stack the profile lists as core (PHP, JavaScript/TypeScript, Vue, Node, React, SQL, REST, full-stack web).",
        "desired_match": "The posting lists the profile's stack only as desired, nice-to-have or a plus, or names it among several acceptable options.",
        "stack_agnostic": "The posting requires no specific stack from the manager, or only generic software-engineering background.",
        "required_mismatch": "The posting requires hands-on depth in a stack the profile marks as aspiration or managed-not-hands-on (for example Java/JVM, Go, .NET, C++, Rust, Kubernetes operations, deep data engineering, embedded).",
    },
}

QUESTIONS = {
    "fit": FIT_QUESTION,
    "verdict": VERDICT_QUESTION,
    "seniority_match": SENIORITY_QUESTION,
    "location_ok": LOCATION_QUESTION,
    "language_blocker": LANGUAGE_QUESTION,
    "team_type": TEAM_TYPE_QUESTION,
    "stack_fit": STACK_FIT_QUESTION,
}

# The choice questions whose answers are stored with every job and shown as its reasons.
REASON_QUESTIONS = ("seniority_match", "location_ok", "language_blocker", "team_type", "stack_fit")
# Answers that explain a skip or a low score at a glance.
BLOCKING_ANSWERS = {
    "seniority_match": {"below", "above"},
    "location_ok": {"no"},
    "language_blocker": {"missing"},
    "team_type": {"platform_infra"},
    "stack_fit": {"required_mismatch"},
}

# Jobs at or above this fit score get the Claude explanation pass (`score --explain`).
EXPLAIN_THRESHOLD = 60

# Description text longer than this is cut before it is sent (keeps requests small; the
# relevant parts of a posting are almost always in the first few thousand characters).
MAX_DESCRIPTION_CHARS = 6000

EXPLAIN_SYSTEM = (
    "You help one candidate decide how to approach a job application. You get the "
    "candidate's profile and one job posting. Answer in short, concrete phrases a recruiter "
    "would recognise; no fluff, no generic advice. Write in English."
)

EXPLAIN_INSTRUCTIONS = (
    "Fill four fields.\n"
    "strong_matches: 2 to 4 concrete points where the profile clearly meets what the posting asks for.\n"
    "gaps: 1 to 3 concrete things the posting asks for that the profile does not show, or shows weakly.\n"
    "red_flags: anything in the posting that should make the candidate cautious (unrealistic scope, "
    "unclear level, mismatch between title and duties, language or location traps). Empty string if none.\n"
    "pitch: one sentence, first person, the candidate could open a cover letter with for this job.\n"
    "Each field is a single string; separate multiple points with '; '."
)
