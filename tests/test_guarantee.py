from datetime import date, timedelta

from fullhouse.core import guarantee
from fullhouse.core.metrics import adr, revpar
from fullhouse.core.models import Client, WeeklyMetrics

START = date(2026, 6, 1)  # a Monday


def make(session, ty_rev, ly_rev, ty_avail=140, ly_avail=140, closures=(), weeks_history=60):
    c = Client(name="T", room_count=20, start_date=START, closure_days=list(closures))
    session.add(c)
    session.commit()
    for i in range(-weeks_history, 14):
        wk = START + timedelta(weeks=i)
        ty = i >= 0
        rev, avail = (ty_rev, ty_avail) if ty else (ly_rev, ly_avail)
        session.add(WeeklyMetrics(client_id=c.id, week_start=wk, revenue=rev, rooms_sold=100,
                                  rooms_available=avail, adr=adr(rev, 100), revpar=revpar(rev, avail)))
    session.commit()
    return c


def test_pass_when_ahead(ctx):
    c = make(ctx.session, ty_rev=11_000, ly_rev=10_000)
    g = guarantee.evaluate(ctx.session, c, 60, today=date(2026, 9, 1))
    assert g.eligible and g.passed is True
    assert g.weeks == 8  # full weeks inside 60 days
    assert g.delta == 8 * 1_000


def test_room_count_increase_raises_the_bar(ctx):
    # 10% more revenue but 25% more rooms: behind on a like-for-like basis.
    c = make(ctx.session, ty_rev=11_000, ly_rev=10_000, ty_avail=175, ly_avail=140)
    g = guarantee.evaluate(ctx.session, c, 90, today=date(2026, 9, 1))
    assert g.capacity_factor == 1.25
    assert g.passed is False and g.delta < 0


def test_closure_days_lower_the_bar(ctx):
    closed = [(START + timedelta(days=d)).isoformat() for d in range(14)]  # two weeks closed
    c = make(ctx.session, ty_rev=8_700, ly_rev=10_000, closures=closed)
    g = guarantee.evaluate(ctx.session, c, 60, today=date(2026, 9, 1))
    assert g.capacity_factor == 0.75  # 2 of 8 weeks closed
    assert g.passed is True


def test_ineligible_without_history(ctx):
    c = make(ctx.session, ty_rev=11_000, ly_rev=10_000, weeks_history=20)
    g = guarantee.evaluate(ctx.session, c, 60, today=date(2026, 9, 1))
    assert not g.eligible and g.passed is None


def test_not_due_yet(ctx):
    c = make(ctx.session, ty_rev=11_000, ly_rev=10_000)
    g = guarantee.evaluate(ctx.session, c, 90, today=START + timedelta(days=30))
    assert g.passed is None and "not reached" in g.note
