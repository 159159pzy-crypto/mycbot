from mybot.adapters.qq.translate import QQ_CAPABILITIES
from mybot.adapters.telegram.translate import TELEGRAM_CAPABILITIES
from mybot.contracts import Citation
from mybot.engine.reply_shaping import EMPTY_REPLY_FALLBACK, shape_reply


def test_short_text_becomes_one_segment() -> None:
    plan = shape_reply("你好!", capabilities=QQ_CAPABILITIES)

    assert plan.text_segments == ("你好!",)
    assert plan.typing.enabled is False


def test_blank_output_falls_back_to_explicit_apology() -> None:
    plan = shape_reply("   \n\n  ", capabilities=TELEGRAM_CAPABILITIES)

    assert plan.text_segments == (EMPTY_REPLY_FALLBACK,)


def test_paragraphs_split_into_at_most_three_segments() -> None:
    text = "one\n\ntwo\n\nthree\n\nfour\n\nfive"

    plan = shape_reply(text, capabilities=TELEGRAM_CAPABILITIES)

    assert len(plan.text_segments) == 3
    assert plan.text_segments[0] == "one"
    assert plan.text_segments[1] == "two"
    assert plan.text_segments[2] == "three\n\nfour\n\nfive"


def test_overlong_segments_truncate_with_ellipsis() -> None:
    plan = shape_reply("x" * 10_000, capabilities=TELEGRAM_CAPABILITIES)

    assert len(plan.text_segments) == 1
    assert len(plan.text_segments[0]) == 3_500
    assert plan.text_segments[0].endswith("…")


def test_typing_follows_capabilities() -> None:
    assert shape_reply("hi", capabilities=TELEGRAM_CAPABILITIES).typing.enabled is True
    assert shape_reply("hi", capabilities=QQ_CAPABILITIES).typing.enabled is False


def test_citations_set_the_plan_field_and_render_a_sources_footer() -> None:
    citations = (
        Citation(label="Weather today", uri="https://a.example/1"),
        Citation(label="https://b.example/2", uri="https://b.example/2"),
    )

    plan = shape_reply("今天多云转晴。", capabilities=TELEGRAM_CAPABILITIES, citations=citations)

    assert plan.citations == citations
    footer = plan.text_segments[-1]
    assert "Sources:" in footer
    assert "[1] Weather today — https://a.example/1" in footer
    assert "[2] https://b.example/2" in footer
    assert "https://b.example/2 — https://b.example/2" not in footer


def test_without_citations_the_output_is_unchanged() -> None:
    plain = shape_reply("hello", capabilities=TELEGRAM_CAPABILITIES)
    explicit = shape_reply("hello", capabilities=TELEGRAM_CAPABILITIES, citations=())

    assert plain == explicit
    assert plain.citations == ()
    assert "Sources:" not in plain.text_segments[-1]


def test_sources_footer_respects_the_segment_cap() -> None:
    citations = tuple(
        Citation(label=f"result {i}", uri=f"https://example.com/{i}") for i in range(10)
    )

    plan = shape_reply("x" * 3_400, capabilities=TELEGRAM_CAPABILITIES, citations=citations)

    assert all(len(segment) <= 3_500 for segment in plan.text_segments)
    assert len(plan.text_segments) <= 3
