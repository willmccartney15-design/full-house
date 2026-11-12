from sqlmodel import select

from fullhouse.agents import funnel_checker, gbp_monitor, listing_auditor, monday_report, rate_scraper, review_responder
from fullhouse.agents.monday_report import ActionItem, Narrative
from fullhouse.core.models import Action, Finding, RateSnapshot, Review
from fullhouse.core.scrapers import ListingData, RoomOffer


def test_rate_scraper_position_table(ctx, client):
    res = rate_scraper.run(ctx, client)
    assert res.ok, res.errors
    pos = res.data["position"]
    assert {r["lead_days"] for r in pos} == {1, 7, 14, 30}
    assert {r["room_type"] for r in pos} == {"double", "twin", "family"}
    for r in pos:
        assert r["market_low"] <= r["market_median"] <= r["market_high"]
    direct = ctx.session.exec(select(RateSnapshot).where(RateSnapshot.ota == "direct")).all()
    assert len(direct) == 12  # 3 rooms x 4 lead times
    # Re-running the same day replaces rather than duplicates.
    rate_scraper.run(ctx, client)
    assert len(ctx.session.exec(select(RateSnapshot).where(RateSnapshot.ota == "direct")).all()) == 12


def test_position_table_math():
    snaps = [
        RateSnapshot(client_id=1, competitor_id=None, date=None, room_type="double", lead_days=7, price=120),
        RateSnapshot(client_id=1, competitor_id=None, date=None, room_type="double", lead_days=7, price=110),
        *[RateSnapshot(client_id=1, competitor_id=i, date=None, room_type="double", lead_days=7, price=p)
          for i, p in enumerate([100, 140, 200], start=1)],
    ]
    [row] = rate_scraper.position_table(snaps)
    assert (row.client_price, row.market_median, row.competitors_priced) == (110, 140, 3)
    assert row.diff_pct == -21.4


def test_rubric_rewards_a_complete_listing():
    weak = ListingData(ota="booking", url="u", photo_count=5, description="x", rooms=[RoomOffer("Room", None)])
    strong = ListingData(ota="booking", url="u", photo_count=45, description="x" * 700, amenities=["a"] * 12,
                         rooms=[RoomOffer("Queen Room, Pool View", 150)], free_cancellation=True,
                         loyalty_program=True, review_score=9.0, review_response_rate=0.9)
    w, s = listing_auditor.rubric(weak), listing_auditor.rubric(strong)
    assert s.score > 90 > 40 > w.score
    assert s.fixes == []
    assert [f.rank for f in w.fixes] == list(range(1, len(w.fixes) + 1))


def test_listing_auditor_stores_audit(ctx, client):
    res = listing_auditor.run(ctx, client)
    audit = res.data["audits"]["booking"]
    assert len(audit["criteria"]) == 8
    assert audit["score"] < 60  # the sample motel's listing is deliberately weak
    assert audit["fixes"][0]["rank"] == 1


def test_review_responder_drafts_and_never_posts(ctx, client):
    res = review_responder.run(ctx, client)
    reviews = ctx.session.exec(select(Review)).all()
    assert res.data["new_reviews"] == 3 and all(r.draft_reply for r in reviews)
    assert not any(r.approved or r.posted for r in reviews)
    negative = next(r for r in reviews if r.rating == 2)
    assert "sorry" in negative.draft_reply.lower()
    assert review_responder.run(ctx, client).data["new_reviews"] == 0  # idempotent import


def test_gbp_monitor_flags_gaps(ctx, client):
    res = gbp_monitor.run(ctx, client)
    texts = " ".join(g["text"] for g in res.data["gaps"])
    assert "unanswered questions" in texts and "No posts" in texts and "Only 9 photos" in texts
    gbp_monitor.run(ctx, client)
    assert len(ctx.session.exec(select(Finding).where(Finding.agent == "gbp_monitor")).all()) == len(res.data["gaps"])


def test_funnel_checker_detects_parity_breach(ctx, client):
    rate_scraper.run(ctx, client)
    res = funnel_checker.run(ctx, client)
    statuses = {r["status"] for r in res.data["rates"]}
    assert "ota_cheaper" in statuses
    assert res.data["site"]["booking_engines"] == ["SiteMinder (TheBookingButton)"]
    issues = " ".join(i["text"] for i in res.data["issues"])
    assert "cheaper than booking direct" in issues and "meta description" in issues


def test_monday_report_three_actions_and_pdf(ctx, client):
    rate_scraper.run(ctx, client)
    funnel_checker.run(ctx, client)
    res = monday_report.run(ctx, client)
    assert res.ok, res.errors
    assert len(res.data["actions"]) == 3
    assert len(ctx.session.exec(select(Action)).all()) == 3
    from pathlib import Path

    assert Path(res.data["pdf"]).stat().st_size > 10_000
    assert Path(res.data["email"]).exists()


def test_monday_report_pads_short_action_lists(ctx, client, monkeypatch):
    short = Narrative(headline="h", narrative="n", actions=[ActionItem(text="only one", why="w")])
    monkeypatch.setattr(ctx.claude, "structured", lambda *a, **k: short)
    data = monday_report.gather(ctx, client, monday_report.monday(ctx.today) - __import__("datetime").timedelta(weeks=1))
    n = monday_report.write(ctx, client, data)
    assert len(n.actions) == 3 and n.actions[0].text == "only one"
