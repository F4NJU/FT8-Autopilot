import pytest

from wsjtx_autopilot.engine.models import CqType, MessageKind
from wsjtx_autopilot.engine.parser import parse_ft8_message


def test_parses_general_cq() -> None:
    message = parse_ft8_message("CQ F1ABC JN18")

    assert message is not None
    assert message.kind is MessageKind.CQ
    assert message.sender == "F1ABC"
    assert message.grid == "JN18"
    assert message.cq_type is CqType.GENERAL


def test_parses_directed_cq_variant() -> None:
    message = parse_ft8_message("CQ DX F1ABC JN18")

    assert message is not None
    assert message.kind is MessageKind.CQ
    assert message.cq_modifier == "DX"
    assert message.cq_type is CqType.OTHER_DIRECTED
    assert message.cq_target == "DX"


def test_parses_positive_report_and_acknowledged_positive_report() -> None:
    report = parse_ft8_message("F4NJU ON4ABC +03")
    acknowledged = parse_ft8_message("F4NJU ON4ABC R+05")

    assert report is not None and report.kind is MessageKind.REPORT and report.report == 3
    assert acknowledged is not None
    assert acknowledged.kind is MessageKind.R_REPORT
    assert acknowledged.report == 5


def test_normalizes_bracketed_nonstandard_callsign() -> None:
    message = parse_ft8_message("UT2UB <II7MGXX> +13")

    assert message is not None
    assert message.sender == "II7MGXX"
    assert message.addressee == "UT2UB"


def test_parses_qrz_candidate() -> None:
    message = parse_ft8_message("QRZ F1ABC JN18")

    assert message is not None
    assert message.kind is MessageKind.QRZ
    assert message.sender == "F1ABC"


def test_parses_call_to_local_station() -> None:
    message = parse_ft8_message("F4NJU ON4ABC -08")

    assert message is not None
    assert message.kind is MessageKind.REPORT
    assert message.is_addressed_to("f4nju")
    assert message.sender == "ON4ABC"
    assert message.report == -8


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("ON4ABC F4NJU RR73", MessageKind.RR73),
        ("ON4ABC F4NJU 73", MessageKind.SEVENTY_THREE),
        ("F4NJU ON4ABC R-08", MessageKind.R_REPORT),
    ],
)
def test_parses_exchange_tokens(text: str, kind: MessageKind) -> None:
    message = parse_ft8_message(text)

    assert message is not None
    assert message.kind is kind


@pytest.mark.parametrize(
    "text",
    ["HELLO FROM PARIS", "CQ NOT-A-CALL JN18", "F4NJU ON4ABC THANKS", "random free text"],
)
def test_rejects_free_text_and_ambiguous_lines(text: str) -> None:
    assert parse_ft8_message(text) is None
