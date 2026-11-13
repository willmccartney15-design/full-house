import email
import smtplib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from fullhouse import digest, onboarding, outreach
from fullhouse.agents import funnel_checker, listing_auditor, monday_report, rate_scraper, review_responder
from fullhouse.core import mailer, proposals
from fullhouse.core.models import Action, ActionStatus, Proposal, ProposalKind, ProposalStatus, Review
from fullhouse.web import create_app


@pytest.fixture
def week(ctx, client):
    """A full weekly run's output for the sample motel."""
    for agent in (rate_scraper, listing_auditor, review_responder, funnel_checker, monday_report):
        agent.run(ctx, client)
    return client


def test_build_creates_one_proposal_per_subject(ctx, week):
    r = proposals.build(ctx.session, week, ctx.today)
    kinds = [p.kind for p in r.created]
    assert kinds.count(ProposalKind.review_reply) == 3
    assert kinds.count(ProposalKind.action) == 3
    assert kinds.count(ProposalKind.listing_fix) == 3
    titles = [" ".join(p.title.lower().split()) for p in r.created]
    assert len(titles) == len(set(titles))  # a finding that is also an action is asked once
    assert proposals.build(ctx.session, week, ctx.today).created == []  # re-running asks nothing new
    top = proposals.pending(ctx.session, week)[0]
    assert top.kind == ProposalKind.review_reply and "2/5" in top.title  # the angry review first


def test_decisions_and_effects(ctx, week):
    proposals.build(ctx.session, week, ctx.today)
    reply = next(p for p in proposals.pending(ctx.session, week) if p.kind == ProposalKind.review_reply)
    proposals.decide(ctx.session, ctx.settings, reply, True, edited_text="Thanks so much. Sam")
    review = ctx.session.get(Review, reply.payload["review_id"])
    assert review.approved and review.draft_reply == "Thanks so much. Sam" and not review.posted
    assert reply.status == ProposalStatus.approved and reply in proposals.todo(ctx.session, week.id)
    with pytest.raises(proposals.DecisionError):
        proposals.decide(ctx.session, ctx.settings, reply, False)
    proposals.mark_done(ctx.session, reply)
    assert ctx.session.get(Review, review.id).posted and reply not in proposals.todo(ctx.session)

    action = next(p for p in proposals.pending(ctx.session, week) if p.kind == ProposalKind.action)
    proposals.decide(ctx.session, ctx.settings, action, False)
    assert ctx.session.get(Action, action.payload["action_id"]).status == ActionStatus.skipped


def test_unanswered_proposals_expire(ctx, week):
    proposals.build(ctx.session, week, ctx.today)
    for p in proposals.pending(ctx.session, week):
        p.created_at = p.created_at - timedelta(days=30)
        ctx.session.add(p)
    ctx.session.commit()
    assert proposals.build(ctx.session, week, ctx.today, ttl_days=14).expired == 9
    assert proposals.pending(ctx.session, week) == []


def test_digest_email(ctx, week):
    proposals.build(ctx.session, week, ctx.today)
    sent = digest.send(ctx.session, ctx.settings, week, send=False)
    msg = email.message_from_bytes(sent.path.read_bytes())
    assert msg["To"] == "sam@coralsands.example" and "9 things to approve" in msg["Subject"]
    html = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    p = proposals.pending(ctx.session, week)[0]
    assert f"/p/{p.token}?d=approve" in html and f"/o/{week.owner_token}" in html
    assert all(x.sent_at for x in proposals.pending(ctx.session, week))


def test_web_app_flow(ctx, week, settings):
    proposals.build(ctx.session, week, ctx.today)
    items = proposals.pending(ctx.session, week)
    web = TestClient(create_app(settings))
    assert web.get(f"/o/{week.owner_token}").status_code == 200
    page = web.get(f"/p/{items[0].token}?d=approve")
    assert page.status_code == 200 and "Approve this?" in page.text
    ctx.session.expire_all()
    assert ctx.session.get(Proposal, items[0].id).status == ProposalStatus.pending  # opening a link decides nothing
    r = web.post(f"/p/{items[0].token}", data={"decision": "approve", "reply": "Edited reply"}, follow_redirects=False)
    assert r.status_code == 303
    ctx.session.expire_all()
    assert ctx.session.get(Proposal, items[0].id).status == ProposalStatus.approved
    assert web.post(f"/p/{items[1].token}", data={"decision": "maybe"}).status_code == 400
    assert web.get("/p/not-a-token").status_code == 404 and web.get("/o/not-a-token").status_code == 404
    assert web.get("/health").json() == {"ok": True}


def test_mailer_outbox_smtp_and_suppression(ctx, monkeypatch):
    s = ctx.settings
    msg = mailer.new_message(s, "owner@motel.example", "Hi", "Body")
    assert mailer.deliver(s, msg, session=ctx.session).reason.startswith("SMTP not configured")

    sent_to = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.example", 587)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            pass

        def login(self, user, password):
            assert (user, password) == ("u", "p")

        def send_message(self, m):
            sent_to.append(m["To"])

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    s.smtp_host, s.smtp_user, s.smtp_password = "smtp.example", "u", "p"
    assert mailer.deliver(s, mailer.new_message(s, "owner@motel.example", "Hi", "Body"), session=ctx.session).sent
    assert sent_to == ["owner@motel.example"]
    mailer.suppress(ctx.session, "Owner@Motel.example")
    r = mailer.deliver(s, mailer.new_message(s, "owner@motel.example", "Hi", "Body"), session=ctx.session)
    assert not r.sent and "unsubscribed" in r.reason and len(sent_to) == 1
    assert not mailer.deliver(s, mailer.new_message(s, "", "Hi", "Body")).sent  # no address, no send


def test_outreach_draft_always_carries_sender_and_unsubscribe(ctx, client):
    from fullhouse import audit
    from fullhouse.sample import SAMPLE_BOOKING_URL

    audit.run(ctx, SAMPLE_BOOKING_URL, website="https://coralsands.example/")
    ctx.settings.sender_contact = "0400 000 000"
    d = outreach.draft(ctx, client)
    assert client.name in d.subject and d.findings and d.attachment and d.attachment.exists()
    r = outreach.deliver(ctx, d, "owner@motel.example", send=False)
    body = email.message_from_bytes(r.path.read_bytes())
    text = next(p for p in body.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    assert "unsubscribe" in text and "Full House" in text and "0400 000 000" in text
    assert any(p.get_content_type() == "application/pdf" for p in body.walk())


def test_onboarding_checklist(ctx, client):
    checks = {c.item: c for c in onboarding.checklist(ctx.session, ctx.settings, client, ctx.today)}
    assert checks["Owner contact email"].done and checks["12 months of PMS history"].done
    assert not checks["Competitor rates (PriceLabs export)"].done
    assert "import-rates" in checks["Competitor rates (PriceLabs export)"].how


def test_weekly_run_ends_with_the_approval_email(settings):
    from fullhouse.scheduler import run_weekly

    results = run_weekly(dry_run=True, settings=settings)
    last = results[1][-1]
    assert last.agent == "approvals" and last.data["created"] == 9 and last.data["email"]


def test_owner_tokens_backfilled_for_old_databases(tmp_path):
    import sqlite3

    from fullhouse.core.db import init_db

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE client (id INTEGER PRIMARY KEY, name VARCHAR, property_type VARCHAR, region VARCHAR, "
                "room_count INTEGER, ota_urls JSON, gbp_url VARCHAR, gbp_place_id VARCHAR, website_url VARCHAR, "
                "pms_type VARCHAR, start_date DATE, voice_profile VARCHAR, closure_days JSON, is_prospect BOOLEAN, "
                "created_at DATETIME)")
    con.execute("INSERT INTO client (id, name) VALUES (1, 'Old Motel')")
    con.commit()
    con.close()
    init_db(f"sqlite:///{db}")
    token = sqlite3.connect(db).execute("SELECT owner_token FROM client").fetchone()[0]
    assert token and len(token) >= 24


def test_monday_report_survives_an_smtp_failure(ctx, client, monkeypatch):
    """Live run, 13 Nov 2026: SMTP was unreachable and the whole report was recorded as failed."""
    from fullhouse.agents import monday_report

    def unreachable(*a, **k):
        raise OSError("[Errno -2] Name or service not known")

    monkeypatch.setattr(smtplib, "SMTP", unreachable)
    ctx.settings.smtp_host = "smtp.example"
    ctx.dry_run = False  # stubs still answer for Claude; only the email goes "live"
    client.contact_email = "owner@motel.example"
    res = monday_report.run(ctx, client)
    assert res.summary and res.data["headline"] == res.summary
    assert len(res.errors) == 1 and "not emailed" in res.errors[0] and "Name or service" in res.errors[0]
    assert res.data["email"] and res.data["email"].endswith(".eml")
