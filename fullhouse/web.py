"""Approvals web app: the owner's one-tap decisions.

    fullhouse approvals serve --host 0.0.0.0 --port 8000

Set FULLHOUSE_APPROVAL_BASE_URL to the address owners reach it at (e.g. https://approve.example.com.au) so the
links in emails point there. Access is by unguessable link only (a token per proposal, and one per owner page).
Links in emails open a confirmation page; only the button press (a POST) records anything, so email scanners that
open links can't approve on the owner's behalf.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, select

from fullhouse.core import proposals
from fullhouse.core.config import Settings, load_settings
from fullhouse.core.db import init_db
from fullhouse.core.models import Client, Proposal, ProposalStatus
from fullhouse.digest import LABELS
from fullhouse.reports.render import render_html

STATUS_WORDS = {
    ProposalStatus.pending: "Waiting for you",
    ProposalStatus.approved: "Approved: we're on it",
    ProposalStatus.skipped: "Skipped",
    ProposalStatus.done: "Done",
    ProposalStatus.expired: "Expired",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = init_db(settings.db_url)
    app = FastAPI(title="Full House approvals", docs_url=None, redoc_url=None, openapi_url=None)

    def db() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    def page(template: str, status: int = 200, **ctx) -> HTMLResponse:
        html = render_html(template, {"labels": LABELS, "status_words": STATUS_WORDS, **ctx})
        return HTMLResponse(html, status_code=status, headers={"X-Robots-Tag": "noindex",
                                                                "Referrer-Policy": "no-referrer",
                                                                "Cache-Control": "no-store"})

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/o/{token}", response_class=HTMLResponse)
    def owner(token: str, session: Session = Depends(db)):
        client = session.exec(select(Client).where(Client.owner_token == token)).first() if token else None
        if client is None:
            return page("web/missing.html", 404, title="Link not valid")
        recent = session.exec(select(Proposal).where(Proposal.client_id == client.id,
                                                     Proposal.status != ProposalStatus.pending)
                              .order_by(Proposal.decided_at.desc())).all()[:15]
        return page("web/owner.html", client=client, pending=proposals.pending(session, client), recent=recent,
                    title=client.name, focus=None)

    @app.get("/p/{token}", response_class=HTMLResponse)
    def show(token: str, request: Request, d: str | None = None, done: int = 0, session: Session = Depends(db)):
        p = proposals.by_token(session, token)
        if p is None:
            return page("web/missing.html", 404, title="Link not valid")
        client = session.get(Client, p.client_id)
        return page("web/proposal.html", p=p, client=client, focus=d if d in ("approve", "skip") else None,
                    just_decided=bool(done), title=p.title)

    @app.post("/p/{token}")
    def decide(token: str, decision: str = Form(...), reply: str | None = Form(None), note: str | None = Form(None),
               session: Session = Depends(db)):
        p = proposals.by_token(session, token)
        if p is None:
            return page("web/missing.html", 404, title="Link not valid")
        if decision not in ("approve", "skip"):
            return JSONResponse({"error": "decision must be approve or skip"}, status_code=400)
        try:
            proposals.decide(session, settings, p, decision == "approve", note=note, edited_text=reply)
        except proposals.DecisionError:
            pass  # already answered (e.g. a second tap): just show its status
        return RedirectResponse(f"/p/{token}?done=1", status_code=303)

    return app
