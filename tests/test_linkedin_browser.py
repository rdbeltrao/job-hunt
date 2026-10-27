from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from jobs.adapters import linkedin_browser as lb
from jobs.browser import is_login_wall
from jobs.seen import load_seen, qualify, save_seen


def test_parse_cards_reads_id_title_company_location_date(fixture_text):
    cards = lb.parse_cards(fixture_text("linkedin_browser_list.html"))

    assert [c.id for c in cards] == ["4477388263", "4477001122", "4479999999"]
    first = cards[0]
    assert first.title == "Teamleitung OSS Software Engineering (w/m/d)"
    assert first.company == "Deutsche Glasfaser Unternehmensgruppe"
    assert first.location == "Düsseldorf, North Rhine-Westphalia, Germany (Hybrid)"
    assert first.posted_date == "2026-10-16"
    assert cards[1].posted_date == "2026-10-15"  # full timestamp trimmed to a date
    assert cards[2].title == "Head of Engineering"  # id and title from href / aria-label only
    assert cards[2].company == "Soar"
    assert cards[2].posted_date == ""


def test_parse_detail_reads_title_company_location_pills_description(fixture_text):
    d = lb.parse_detail(fixture_text("linkedin_browser_detail.html"))

    assert d.title == "Engineering Manager Platform (m/f/d)"
    assert d.company == "momox"
    assert d.location == "Berlin, Germany"
    assert d.workplace == "hybrid"
    assert d.employment_type == "Full-time"
    assert d.seniority == "Mid-Senior level"
    assert d.salary == "€90K/yr - €110K/yr"
    assert d.pills == ["€90K/yr - €110K/yr", "Hybrid", "Full-time", "Mid-Senior level"]
    assert d.description.startswith("Your mission")
    assert "Kubernetes" in d.description
    assert "- Grow and coach the team" in d.description
    assert "<" not in d.description
    assert "About the job" not in d.description


def test_parse_detail_on_empty_html_returns_blank_detail():
    d = lb.parse_detail("<div></div>")
    assert d.title == "" and d.description == "" and d.workplace is None and d.pills == []
    assert d.company_url == "" and d.contact_name == "" and d.contact_title == "" and d.contact_url == ""


def test_parse_detail_reads_company_page_from_top_card(fixture_text):
    d = lb.parse_detail(fixture_text("linkedin_browser_detail.html"))
    assert d.company_url == "https://www.linkedin.com/company/momox/"  # "/life" tab dropped

    real = lb.parse_detail(fixture_text("linkedin_browser_detail_real.html"), now=NOW)
    assert real.company_url == "https://www.linkedin.com/company/gtu-mobility-gmbh-&-co.-kg/"


def test_parse_detail_without_hiring_team_leaves_contact_blank(fixture_text):
    d = lb.parse_detail(fixture_text("linkedin_browser_detail.html"))
    assert d.contact_name == "" and d.contact_title == "" and d.contact_url == ""


HIRER_CARD_HTML = """
<div class="scaffold-layout__detail">
  <h1><a href="/jobs/view/4477388263/">Engineering Manager</a></h1>
  <div class="job-details-jobs-unified-top-card__company-name">
    <a class="app-aware-link" href="https://www.linkedin.com/company/acme-gmbh/life?trk=x">Acme GmbH</a>
  </div>
  <div id="job-details"><p>About the job Build things.</p></div>
  <div class="job-details-people-who-can-help__section--two-pane">
    <h2 class="text-heading-medium mb4">Meet the hiring team</h2>
    <div class="display-flex align-items-center mt4">
      <a class="app-aware-link" href="https://www.linkedin.com/in/jane-doe?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AABC" target="_self">
        <img alt="Jane Doe" src="https://media.licdn.com/x.jpg">
      </a>
      <div class="hirer-card__hirer-information">
        <a class="app-aware-link" href="https://www.linkedin.com/in/jane-doe?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AABC" target="_self">
          <strong>Jane Doe</strong>
          <span class="hirer-card__connection-degree"> · 3rd</span>
        </a>
        <div class="hirer-card__hirer-job-title t-14 t-black--light">Talent Acquisition Manager at Acme GmbH</div>
        <div class="hirer-card__hirer-job-poster t-12">Job poster</div>
      </div>
      <div class="hirer-card__message-button">
        <button aria-label="Message Jane Doe" class="artdeco-button"><span class="artdeco-button__text">Message</span></button>
      </div>
    </div>
  </div>
  <div class="job-details-similar-jobs">
    <a href="https://www.linkedin.com/company/other-co/">Other Co</a>
    <a href="https://www.linkedin.com/in/someone-else/">Someone Else</a>
  </div>
</div>
"""


def test_parse_detail_reads_hiring_team_contact_and_message_target():
    d = lb.parse_detail(HIRER_CARD_HTML, now=NOW)

    assert d.company_url == "https://www.linkedin.com/company/acme-gmbh/"  # top card wins over similar jobs
    assert d.contact_name == "Jane Doe"  # connection degree dropped
    assert d.contact_title == "Talent Acquisition Manager at Acme GmbH"
    assert d.contact_url == "https://www.linkedin.com/in/jane-doe/"  # miniProfileUrn query dropped


def test_parse_detail_reads_older_jobs_poster_markup():
    html = """
    <div class="jobs-details">
      <div class="jobs-poster">
        <div class="jobs-poster__wrapper">
          <a href="/in/max-mustermann/" class="jobs-poster__name-link">
            <span class="jobs-poster__name">Max Mustermann</span>
          </a>
          <div class="jobs-poster__headline">Engineering Lead at Beta</div>
          <button>Message</button>
        </div>
      </div>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.contact_name == "Max Mustermann"
    assert d.contact_title == "Engineering Lead at Beta"
    assert d.contact_url == "https://www.linkedin.com/in/max-mustermann/"  # relative href gets the LinkedIn origin


def test_parse_detail_ignores_people_you_can_reach_out_to_without_a_hirer_card():
    html = """
    <div class="scaffold-layout__detail">
      <div class="job-details-people-who-can-help__section">
        <h2>People you can reach out to</h2>
        <a href="https://www.linkedin.com/in/old-colleague/"><strong>Old Colleague</strong></a>
        <div class="t-black--light">Engineer at Acme</div>
      </div>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.contact_name == "" and d.contact_url == ""


OBFUSCATED_2026_HTML = """
<div class="l4jlpg l4ju5">
  <div class="l4jax7 l4jln8">
    <a class="l4jlyw" href="https://www.linkedin.com/company/intact-gmbh/life/"><p class="l4jk8d">Intact</p></a>
    <h1 class="l4jk8d">Engineering Manager</h1>
  </div>
  <div class="l4jlpg l4jk1g"><div class="l4jkyq"><a href="https://www.linkedin.com/company/intact-gmbh/life/">Intact 9,816 followers</a></div></div>
  <div class="l4jlpg l4ju5">
    <div class="l4jlpg l4jkzo">
      <h2 class="l4jk8d">People you can reach out to</h2>
      <div class="l4jlpg l4jayf">
        <p class="l4jk8d">Meet the hiring team</p>
        <div class="l4jayf l4jij2">
          <a class="l4jikq l4jlyw" href="https://www.linkedin.com/in/barbara-example-40b29a20a/" tabindex="0">
            <div class="l4jikq"><figure><img alt="" src="x.jpg"></figure>
              <div class="l4jlpg"><div class="l4jayf"><div class="l4jk0d"><p class="l4jk8d"><span>Barbara Example</span></p><p class="l4jk8d">• 3rd</p></div></div>
                <div class="l4jk0d"><p class="l4jk8d"><span>HR-Generalist bei Intact</span></p></div>
                <p class="l4jk8d">Job poster</p>
              </div>
            </div>
          </a>
          <div class="l4jlpg"><button class="l4jbtn"><span>Message</span></button></div>
        </div>
      </div>
    </div>
  </div>
  <div class="l4jlpg"><h2>Similar jobs</h2><a href="https://www.linkedin.com/company/other-co/life/">Other Co</a><a href="https://www.linkedin.com/in/someone/">Someone</a></div>
</div>
"""


def test_parse_detail_reads_company_and_hiring_team_from_the_2026_obfuscated_markup():
    """Shape of a job page saved with `backfill --dump-html` on 2026-10-24: no semantic class names at all."""
    d = lb.parse_detail(OBFUSCATED_2026_HTML, now=NOW)

    assert d.company_url == "https://www.linkedin.com/company/intact-gmbh/"  # first company link, not Similar jobs
    assert d.contact_name == "Barbara Example"
    assert d.contact_title == "HR-Generalist bei Intact"
    assert d.contact_url == "https://www.linkedin.com/in/barbara-example-40b29a20a/"


def test_parse_detail_2026_markup_without_hiring_team_keeps_company_and_no_contact():
    html = OBFUSCATED_2026_HTML.split('<div class="l4jlpg l4ju5">\n    <div class="l4jlpg l4jkzo">')[0] + "</div>"
    d = lb.parse_detail(html, now=NOW)
    assert d.company_url == "https://www.linkedin.com/company/intact-gmbh/"
    assert d.contact_name == "" and d.contact_url == ""


def test_parse_detail_hirer_card_without_profile_link_is_not_a_contact():
    html = """
    <div class="scaffold-layout__detail">
      <div class="job-details-people-who-can-help__section">
        <h2>Meet the hiring team</h2>
        <div class="hirer-card__hirer-information"><strong>LinkedIn Member</strong>
          <div class="hirer-card__hirer-job-title">Recruiter</div>
        </div>
      </div>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.contact_name == "" and d.contact_title == "" and d.contact_url == ""


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://www.linkedin.com/company/momox/life", "https://www.linkedin.com/company/momox/"),
        ("https://www.linkedin.com/company/momox/?trk=x", "https://www.linkedin.com/company/momox/"),
        ("https://de.linkedin.com/company/acme-gmbh", "https://de.linkedin.com/company/acme-gmbh/"),
        ("/company/acme-gmbh/jobs/", "https://www.linkedin.com/company/acme-gmbh/"),
        ("https://www.linkedin.com/jobs/view/1/", ""),
        ("", ""),
    ],
)
def test_canonical_company_url(href, expected):
    assert lb.canonical_company_url(href) == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://www.linkedin.com/in/jane-doe?miniProfileUrn=abc", "https://www.linkedin.com/in/jane-doe/"),
        ("/in/jane-doe/", "https://www.linkedin.com/in/jane-doe/"),
        ("https://www.linkedin.com/company/acme/", ""),
        ("", ""),
    ],
)
def test_canonical_profile_url(href, expected):
    assert lb.canonical_profile_url(href) == expected


def test_build_job_merges_card_and_detail(fixture_text):
    card = lb.parse_cards(fixture_text("linkedin_browser_list.html"))[1]
    detail = lb.parse_detail(fixture_text("linkedin_browser_detail.html"))

    job = lb.build_job(card, detail, "em-germany-24h")

    assert job.source == "linkedin"
    assert job.source_id == "4477001122"
    assert job.url == "https://www.linkedin.com/jobs/view/4477001122/"
    assert job.workplace == "hybrid"  # detail pill wins over "(Remote)" in the card location
    assert job.seniority == "Mid-Senior level"
    assert job.salary == "€90K/yr - €110K/yr"
    assert job.posted_date == "2026-10-15"
    assert job.extra["search"] == "em-germany-24h"
    assert job.extra["employment_type"] == "Full-time"
    assert job.company_url == "https://www.linkedin.com/company/momox/"
    assert job.contact_name == "" and job.contact_url == ""


def test_build_job_carries_company_page_and_contact(fixture_text):
    card = lb.parse_cards(fixture_text("linkedin_browser_list.html"))[0]
    detail = lb.parse_detail(HIRER_CARD_HTML, now=NOW)

    card = replace(card, title=detail.title, company=detail.company)
    job = lb.build_job(card, detail, "s")

    assert job.company_url == "https://www.linkedin.com/company/acme-gmbh/"
    assert job.contact_name == "Jane Doe"
    assert job.contact_title == "Talent Acquisition Manager at Acme GmbH"
    assert job.contact_url == "https://www.linkedin.com/in/jane-doe/"
    assert job.to_dict()["contact_url"] == "https://www.linkedin.com/in/jane-doe/"  # lands in jobs.jsonl


def test_build_job_without_detail_falls_back_to_card_and_text_heuristic(fixture_text):
    card = lb.parse_cards(fixture_text("linkedin_browser_list.html"))[1]
    job = lb.build_job(card, None, "s")
    assert job.title == "Engineering Manager Platform (m/f/d)"
    assert job.workplace == "remote"
    assert job.description == ""


def test_with_start_sets_and_clears_paging_param():
    url = "https://www.linkedin.com/jobs/search/?keywords=Engineering%20Manager&geoId=101282230&currentJobId=1&start=50"
    assert lb.with_start(url, 25) == "https://www.linkedin.com/jobs/search/?keywords=Engineering+Manager&geoId=101282230&start=25"
    assert lb.with_start(url, 0) == "https://www.linkedin.com/jobs/search/?keywords=Engineering+Manager&geoId=101282230"


def test_login_wall_detection():
    assert is_login_wall("https://www.linkedin.com/login?session_redirect=%2Fjobs")
    assert is_login_wall("https://www.linkedin.com/checkpoint/challenge/abc")
    assert is_login_wall("https://www.linkedin.com/authwall?trk=x")
    assert is_login_wall("https://www.linkedin.com/uas/login-submit")
    assert not is_login_wall("https://www.linkedin.com/jobs/search/?keywords=x")
    assert not is_login_wall("https://www.linkedin.com/feed/")


def test_seen_ids_roundtrip(tmp_path):
    path = tmp_path / "seen_ids.json"
    assert load_seen(path) == set()
    save_seen({qualify("linkedin", "2"), qualify("linkedin", "1")}, path)
    assert load_seen(path) == {"linkedin:1", "linkedin:2"}
    assert path.read_text().startswith("[")


def test_seen_ids_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "seen_ids.json"
    path.write_text("{not json")
    assert load_seen(path) == set()


def test_fixture_files_exist():
    assert (Path(__file__).parent / "fixtures" / "linkedin_browser_list.html").exists()


# ---- real markup saved with `fetch --dump-html` on 2026-10-16

NOW = datetime(2026, 10, 16, 18, 30)


def test_parse_cards_real_markup_reads_time_tag_and_leaves_promoted_cards_blank(fixture_text):
    cards = lb.parse_cards(fixture_text("linkedin_browser_list_real.html"))

    assert [c.id for c in cards] == ["4480393707", "4479350977"]
    assert cards[0].title == "Engineering Manager (m/w/d) - INNSIDE by Meliá München Parkstadt Schwabing"
    assert cards[0].company == "Meliá Hotels International"
    assert cards[0].location == "Munich, Bavaria, Germany (On-site)"
    assert cards[0].posted_date == "2026-10-16"  # <time datetime="2026-10-16">15 minutes ago</time>
    assert cards[1].title == "Engineering Manager (m/f/d)"
    assert cards[1].company == "TOPdesk"
    assert cards[1].salary == "7,500 EUR/month - 8,500 EUR/month"
    assert cards[1].posted_date == ""  # promoted cards carry no date; the detail pane fills it


def test_parse_detail_real_markup_derives_posted_date_from_reposted_text(fixture_text):
    d = lb.parse_detail(fixture_text("linkedin_browser_detail_real.html"), now=NOW)

    assert d.title == "Teamleiter Planung Leit- und Sicherungstechnik Bahn (m/w/d)"
    assert d.company == "GTU Mobility GmbH & Co. KG"
    assert d.location == "Cologne, North Rhine-Westphalia, Germany"
    assert d.posted_date == "2026-10-15"  # "Reposted 23 hours ago" at 18:30 is the day before
    assert d.workplace == "onsite"
    assert d.employment_type == "Full-time"
    assert d.pills == ["On-site", "Full-time"]
    assert "Köln" in d.description and "Die GTU Unternehmensgruppe" in d.description
    assert "<" not in d.description


def test_parse_detail_ignores_premium_applicant_seniority_insight(fixture_text):
    """'Applicant seniority level: Entry level applicants' describes applicants, not the job."""
    html = fixture_text("linkedin_browser_detail_real.html")
    assert "Applicant seniority level" in html and "Entry level applicants" in html

    d = lb.parse_detail(html, now=NOW)

    assert d.seniority == ""


def test_parse_detail_reads_seniority_from_job_criteria_list():
    html = """
    <div class="scaffold-layout__detail">
      <h1 class="t-24">Head of Engineering</h1>
      <ul class="description__job-criteria-list">
        <li class="description__job-criteria-item">
          <h3 class="description__job-criteria-subheader">Seniority level</h3>
          <span class="description__job-criteria-text">Director</span>
        </li>
        <li class="description__job-criteria-item">
          <h3 class="description__job-criteria-subheader">Employment type</h3>
          <span class="description__job-criteria-text">Full-time</span>
        </li>
      </ul>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.seniority == "Director"


def test_parse_detail_pill_seniority_wins_over_criteria(fixture_text):
    d = lb.parse_detail(fixture_text("linkedin_browser_detail.html"), now=NOW)
    assert d.seniority == "Mid-Senior level"
    assert d.posted_date == "2026-10-15"  # "1 day ago"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Berlin, Germany · 15 minutes ago · 0 people clicked apply", "2026-10-16"),
        ("Munich, Bavaria, Germany · Reposted 21 hours ago · 81 applicants", "2026-10-15"),
        ("Cologne · Reposted 1 hour ago", "2026-10-16"),
        ("Hamburg · 2 days ago · 12 applicants", "2026-10-14"),
        ("EMEA · 1 week ago", "2026-10-09"),
        ("EMEA · 3 weeks ago", "2026-09-25"),
        ("Remote · 1 month ago", "2026-09-16"),
        ("an hour ago", "2026-10-16"),
        ("Just now", "2026-10-16"),
        ("yesterday", "2026-10-15"),
        ("Posted on Oct 14, 2026", "2026-10-14"),
        ("Berlin · 14 Oct 2026 · 3 applicants", "2026-10-14"),
        ("Berlin · 2026-10-13", "2026-10-13"),
        ("Berlin, Germany · 45 applicants", ""),
        ("", ""),
    ],
)
def test_posted_date_from_text(text, expected):
    assert lb.posted_date_from_text(text, now=NOW) == expected


def test_build_job_uses_detail_posted_date_when_card_has_none(fixture_text):
    card = lb.parse_cards(fixture_text("linkedin_browser_list_real.html"))[1]
    detail = lb.parse_detail(fixture_text("linkedin_browser_detail_real.html"), now=NOW)
    assert card.posted_date == ""
    card = replace(card, id=detail.job_id, title=detail.title, company=detail.company)

    job = lb.build_job(card, detail, "em-germany-24h")

    assert job.posted_date == "2026-10-15"
    assert job.seniority == ""
    assert job.salary == "7,500 EUR/month - 8,500 EUR/month"  # card salary survives when the pane has none


def test_build_job_prefers_card_time_tag_over_detail_text(fixture_text):
    card = lb.parse_cards(fixture_text("linkedin_browser_list_real.html"))[0]
    detail = lb.parse_detail(fixture_text("linkedin_browser_detail_real.html"), now=NOW)

    card = replace(card, id=detail.job_id, title=detail.title, company=detail.company)
    job = lb.build_job(card, detail, "s")

    assert job.posted_date == "2026-10-16"


def test_parse_detail_recognises_monthly_eur_pill_as_salary():
    html = """
    <div class="job-details-fit-level-preferences">
      <button><span><strong>7,500 EUR/month - 8,500 EUR/month</strong></span></button>
      <button><span><strong>Hybrid</strong></span><span class="visually-hidden">Matches your job preferences, workplace type is Hybrid.</span></button>
      <button><span><strong>Full-time</strong></span></button>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.salary == "7,500 EUR/month - 8,500 EUR/month"
    assert d.workplace == "hybrid"
    assert d.employment_type == "Full-time"
    assert d.pills == ["7,500 EUR/month - 8,500 EUR/month", "Hybrid", "Full-time"]


def test_parse_detail_ignores_description_bullets_that_merely_mention_seniority():
    html = """
    <div id="job-details">
      <ul>
        <li><strong>Seniority &amp; Leadership:</strong> <span>You have led teams of 5+ engineers.</span></li>
        <li><strong>Seniority level:</strong> Director</li>
      </ul>
    </div>
    """
    d = lb.parse_detail(html, now=NOW)
    assert d.seniority == "Director"

    d2 = lb.parse_detail('<div id="job-details"><ul><li><strong>Seniority &amp; Leadership:</strong> <span>Led teams.</span></li></ul></div>', now=NOW)
    assert d2.seniority == ""


@pytest.mark.parametrize('change', [
    {'job_id': '4380699028'}, {'job_id': ''},
    {'company': 'TEN Thüringer Energienetze'}, {'title': 'Another engineering role'},
    {'description': ''},
])
def test_build_job_refuses_unverified_or_conflicting_details(fixture_text, change):
    card = lb.parse_cards(fixture_text('linkedin_browser_list.html'))[1]
    detail = lb.parse_detail(fixture_text('linkedin_browser_detail.html'))
    with pytest.raises(lb.WrongPosting):
        lb.build_job(card, replace(detail, **change), 'search')


def test_matching_recommendation_link_cannot_validate_wrong_primary_heading():
    html = '''<h1><a href="/jobs/view/111/">Wrong job</a></h1>
    <aside><h2><a href="/jobs/view/222/">Wanted job</a></h2></aside>'''
    assert lb.parse_detail(html).job_id == '111'


def test_modern_posting_identity_is_scoped_to_primary_content():
    html = '''<title>Engineering Manager | Doctolib | LinkedIn</title>
    <main><section aria-label="Primary content">
    <div id="JobDetails_ManageJobBanner_222"></div>
    <h2>About the job</h2><span data-testid="expandable-text-box">Lead engineers.</span>
    </section><aside><div id="JobDetails_ManageJobBanner_111"></div></aside></main>'''
    detail = lb.parse_detail(html)
    assert detail.job_id == '222'
    assert detail.title == 'Engineering Manager'
    assert detail.company == 'Doctolib'
    assert detail.description == 'Lead engineers.'
    lb.validate_detail(lb.Card(id='222', title=detail.title, company=detail.company), detail)
    assert lb.parse_detail(html.replace('JobDetails_ManageJobBanner_222', 'unknown')).job_id == ''


def test_open_detail_uses_fresh_document_and_always_closes_it(monkeypatch, fixture_text):
    from types import SimpleNamespace
    html = fixture_text('linkedin_browser_detail.html')
    events = []
    posting = SimpleNamespace(
        goto=lambda url, **kw: events.append(('goto', url)),
        wait_for_selector=lambda *a, **kw: None,
        wait_for_timeout=lambda *a: None,
        content=lambda: html,
        close=lambda: events.append(('close',)),
    )
    page = SimpleNamespace(context=SimpleNamespace(new_page=lambda: posting))
    monkeypatch.setattr(lb, '_assert_logged_in', lambda p: None)
    assert lb._open_detail(page, '4477001122') == html
    assert events == [('goto', 'https://www.linkedin.com/jobs/view/4477001122/'), ('close',)]
    with pytest.raises(lb.WrongPosting):
        lb._open_detail(page, '4380699028')
    assert events[-1] == ('close',)


def test_fetch_keeps_card_without_poisoning_description_on_identity_failure(monkeypatch, fixture_text):
    from types import SimpleNamespace
    page = SimpleNamespace(goto=lambda *a, **kw: None, wait_for_timeout=lambda *a: None)
    context = SimpleNamespace(pages=[page])
    for name in ['_assert_logged_in', '_wait_for_cards', '_scroll_results', '_sleep']:
        monkeypatch.setattr(lb, name, lambda *a: None)
    monkeypatch.setattr(lb, '_results_html', lambda p: fixture_text('linkedin_browser_list.html'))
    monkeypatch.setattr(lb, '_open_detail', lambda *a: fixture_text('linkedin_browser_detail.html'))
    jobs = lb.fetch(context, 'https://www.linkedin.com/jobs/search/', 'search', max_pages=1)
    assert len(jobs) == 3
    assert [bool(job.description) for job in jobs] == [False, True, False]
    assert jobs[0].company == 'Deutsche Glasfaser Unternehmensgruppe'
