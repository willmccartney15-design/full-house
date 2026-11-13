import httpx
import anthropic
import pytest
from pydantic import BaseModel
from sqlmodel import select

from fullhouse.core.claude import ClaudeClient, ClaudeRefusal, _strict_schema, estimate_cost
from fullhouse.core.models import ClaudeCall


class Usage:
    input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 1000, 500, 0, 0


class Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class Response:
    def __init__(self, text, stop="end_turn"):
        self.content, self.stop_reason, self.usage, self.model, self.stop_details = [Block(text)], stop, Usage(), "claude-opus-5-5", None


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeAnthropic:
    def __init__(self, outcomes):
        self.messages = FakeMessages(outcomes)
        self.beta = type("B", (), {"messages": self.messages})()


def rate_limited():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req, headers={"retry-after": "1"}),
                                    body=None)


class Score(BaseModel):
    title: str
    score: int


def test_retries_then_logs_cost(ctx):
    fake = FakeAnthropic([rate_limited(), Response('{"title": "ok", "score": 7}')])
    sleeps = []
    claude = ClaudeClient(ctx.settings, session=ctx.session, client=fake, sleep=sleeps.append)
    out = claude.structured("sys", "prompt", Score, agent="t", client_id=1)
    assert out == Score(title="ok", score=7)
    assert sleeps == [1.0]
    call = fake.messages.calls[-1]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    row = ctx.session.exec(select(ClaudeCall)).one()
    assert row.attempts == 2 and row.cost_usd == pytest.approx(estimate_cost("claude-opus-5-5", Usage()))
    assert row.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


def test_refusal_raises(ctx):
    claude = ClaudeClient(ctx.settings, session=ctx.session, client=FakeAnthropic([Response("", stop="refusal")]))
    with pytest.raises(ClaudeRefusal):
        claude.text("s", "p", agent="t")


def test_dry_run_uses_stub_and_never_calls(ctx):
    claude = ClaudeClient(ctx.settings, dry_run=True, client=FakeAnthropic([]))
    assert claude.text("s", "p", agent="t", stub=lambda: "stubbed") == "stubbed"


def test_strict_schema_keeps_field_named_title():
    js = _strict_schema(Score)
    assert js["additionalProperties"] is False
    assert set(js["required"]) == {"title", "score"}
    assert "title" in js["properties"]


def test_project_key_name_takes_precedence(ctx, monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, api_key=None, **kw):
            seen["api_key"] = api_key

    monkeypatch.setenv("FULLHOUSE_ANTHROPIC_API_KEY", "sk-project")
    monkeypatch.setattr(anthropic, "Anthropic", Recorder)
    ClaudeClient(ctx.settings).client
    assert seen["api_key"] == "sk-project"
    monkeypatch.delenv("FULLHOUSE_ANTHROPIC_API_KEY")
    ClaudeClient(ctx.settings).client
    assert seen["api_key"] is None  # the SDK then reads ANTHROPIC_API_KEY itself
