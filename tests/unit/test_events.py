"""Line assembly and event parsing: the typed event families, a replayed
real-shaped `apply -json` stream, forward-compatibility with unknown
types/values, and the `LineAssembler` chunking/truncation rules."""

from __future__ import annotations

import pytest

from terrapy.enums import Level
from terrapy.events import (
    DEFAULT_MAX_LINE_BYTES,
    EVENT_REGISTRY,
    ApplyCompleteEvent,
    ApplyStartEvent,
    ChangeSummaryEvent,
    DiagnosticEvent,
    Event,
    LineAssembler,
    MalformedLineEvent,
    OutputsEvent,
    PlannedChangeEvent,
    ProvisionStartEvent,
    ResourceDriftEvent,
    VersionEvent,
    event_from_line,
    parse_event_line,
)

# -- typed event families -----------------------------------------------------


def test_parse_version_event() -> None:
    ev = parse_event_line('{"type":"version","terraform":"1.9.5","ui":"1.2","@level":"info"}')
    assert isinstance(ev, VersionEvent)
    assert ev.terraform_version == "1.9.5"
    assert ev.ui_version == "1.2"
    assert ev.level is Level.INFO


def test_diagnostic_event_parses_nested_diagnostic() -> None:
    ev = parse_event_line(
        '{"type":"diagnostic","@message":"bad","diagnostic":'
        '{"severity":"error","summary":"Reference to undeclared resource"}}'
    )
    assert isinstance(ev, DiagnosticEvent)
    assert ev.diagnostic.summary == "Reference to undeclared resource"


def test_diagnostic_event_tolerates_missing_diagnostic() -> None:
    ev = parse_event_line('{"type":"diagnostic","@message":"bad"}')
    assert isinstance(ev, DiagnosticEvent)
    assert ev.diagnostic.summary == ""


def test_planned_change_event_reads_change_fields() -> None:
    ev = parse_event_line(
        '{"type":"planned_change","@message":"plan","change":'
        '{"resource":{"addr":"aws_instance.web"},"action":"create","reason":""}}'
    )
    assert isinstance(ev, PlannedChangeEvent)
    assert ev.resource.addr == "aws_instance.web"
    assert ev.action.value == "create"


def test_planned_change_defaults_action_to_noop() -> None:
    ev = parse_event_line('{"type":"planned_change","@message":"plan","change":{}}')
    assert isinstance(ev, PlannedChangeEvent)
    assert ev.action.value == "no-op"


def test_resource_drift_event_reads_previous_run_action() -> None:
    ev = parse_event_line(
        '{"type":"resource_drift","@message":"drift","change":'
        '{"resource":{"addr":"aws_vpc.main"},"action":"update",'
        '"previous_run_action":"create"}}'
    )
    assert isinstance(ev, ResourceDriftEvent)
    assert ev.previous_run_action == "create"
    assert ev.resource.addr == "aws_vpc.main"


def test_provision_start_event_reads_provisioner() -> None:
    ev = parse_event_line(
        '{"type":"provision_start","@message":"provisioning","hook":'
        '{"resource":{"addr":"aws_instance.web"},"provisioner":"remote-exec"}}'
    )
    assert isinstance(ev, ProvisionStartEvent)
    assert ev.provisioner == "remote-exec"


# -- tolerance ----------------------------------------------------------------


def test_parse_malformed_line() -> None:
    ev = parse_event_line("not json at all {{{")
    assert isinstance(ev, MalformedLineEvent)
    assert ev.raw["text"] == "not json at all {{{"


@pytest.mark.parametrize("line", ["[1, 2, 3]", '"a string"', "   "])
def test_non_object_json_and_blank_lines_are_malformed(line: str) -> None:
    assert isinstance(parse_event_line(line), MalformedLineEvent)


def test_unknown_type_falls_back_to_base_event() -> None:
    ev = parse_event_line('{"type":"totally_new_message_type","@message":"hi"}')
    assert type(ev) is Event
    assert ev.type == "totally_new_message_type"
    assert ev.message == "hi"


def test_parser_failure_falls_back_to_base_event(monkeypatch) -> None:
    # Every real `from_dict` is deliberately tolerant, so no wire payload can
    # make one raise. Force it, to prove the defensive fallback works rather
    # than letting the exception escape into the reader thread.
    def exploding(_payload):
        raise ValueError("parser is broken")

    monkeypatch.setitem(EVENT_REGISTRY, "diagnostic", exploding)
    ev = parse_event_line('{"type":"diagnostic","@message":"x"}')
    assert type(ev) is Event
    assert ev.message == "x"


def test_tolerant_parsers_do_not_need_the_fallback() -> None:
    # The flip side: a badly shaped payload for a known type still yields the
    # typed event, with the bad field defaulted.
    ev = parse_event_line('{"type":"diagnostic","@message":"x","diagnostic":12345}')
    assert isinstance(ev, DiagnosticEvent)
    assert ev.diagnostic.summary == ""


def test_unknown_level_is_tolerated() -> None:
    ev = parse_event_line('{"type":"log","@level":"catastrophic","@message":"uh oh"}')
    assert ev.level.value == "catastrophic"
    assert ev.level == "catastrophic"


def test_replay_apply_stream_fixture(jsonl_fixture) -> None:
    lines = jsonl_fixture("apply_stream.jsonl")
    events = [parse_event_line(line) for line in lines]

    assert isinstance(events[0], VersionEvent)
    assert events[0].terraform_version == "1.9.5"

    starts = [e for e in events if isinstance(e, ApplyStartEvent)]
    completes = [e for e in events if isinstance(e, ApplyCompleteEvent)]
    assert [e.address for e in starts] == ["module.network.aws_vpc.main", "aws_instance.web[0]"]
    assert completes[0].id_value == "vpc-0abc123"
    assert completes[1].id_value == "i-0def456"
    assert completes[1].elapsed_seconds == 5

    summaries = [e for e in events if isinstance(e, ChangeSummaryEvent)]
    assert summaries[0].summary.add == 3
    assert summaries[0].summary.change == 1

    outputs = [e for e in events if isinstance(e, OutputsEvent)]
    assert outputs[0].outputs["vpc_id"].value == "vpc-0abc123"
    assert outputs[0].outputs["db_password"].sensitive is True


# -- line assembly ------------------------------------------------------------


class TestLineAssembler:
    def test_splits_multiple_lines_in_one_chunk(self) -> None:
        assembler = LineAssembler()
        lines = assembler.feed(b'{"a":1}\n{"b":2}\n')
        assert [line.text for line in lines] == ['{"a":1}', '{"b":2}']
        assert all(not line.truncated for line in lines)

    def test_joins_a_line_split_across_chunks(self) -> None:
        assembler = LineAssembler()
        assert [x.text for x in assembler.feed(b'{"a":1}\n{"b"')] == ['{"a":1}']
        assert [x.text for x in assembler.feed(b':2}\n')] == ['{"b":2}']
        assert assembler.flush() is None

    def test_handles_split_multibyte_utf8(self) -> None:
        assembler = LineAssembler()
        text = '{"@message":"café"}\n'.encode()
        first, second = text[:-3], text[-3:]
        assert assembler.feed(first) == []
        lines = assembler.feed(second)
        assert len(lines) == 1
        assert "café" in lines[0].text

    def test_strips_trailing_cr(self) -> None:
        assembler = LineAssembler()
        lines = assembler.feed(b'{"a":1}\r\n')
        assert lines[0].text == '{"a":1}'

    def test_strips_leading_bom_once(self) -> None:
        assembler = LineAssembler()
        lines = assembler.feed('﻿{"a":1}\n﻿{"a":2}\n'.encode())
        assert lines[0].text == '{"a":1}'
        # Only the stream's first line carries a BOM; a later one is content.
        assert lines[1].text == '﻿{"a":2}'

    def test_flush_returns_trailing_unterminated_line(self) -> None:
        assembler = LineAssembler()
        assert assembler.feed(b'{"a":1}') == []
        trailing = assembler.flush()
        assert trailing is not None
        assert trailing.text == '{"a":1}'
        assert assembler.flush() is None

    def test_flush_with_nothing_pending_returns_none(self) -> None:
        assembler = LineAssembler()
        assembler.feed(b'{"a":1}\n')
        assert assembler.flush() is None

    def test_flush_ignores_a_blank_remainder(self) -> None:
        assembler = LineAssembler()
        assembler.feed(b"\r")
        assert assembler.flush() is None

    def test_max_line_bytes_truncates_and_resyncs(self) -> None:
        assembler = LineAssembler(max_line_bytes=10)
        out = assembler.feed(b"x" * 50 + b"\n" + b'{"a":1}\n')
        assert out[0].truncated is True
        assert out[0].text == ""  # the over-long text is discarded, not kept
        assert out[1].text == '{"a":1}'
        assert out[1].truncated is False

    def test_resyncs_after_an_overflow_with_no_newline(self) -> None:
        # Distinct from the case above: overflowing *while still buffering*
        # sets the discard flag, and the next newline must close out the
        # dropped line before normal lines resume.
        assembler = LineAssembler(max_line_bytes=10)
        assert assembler.feed(b"y" * 64) == []
        out = assembler.feed(b"\nshort\n")
        assert [(x.text, x.truncated) for x in out] == [("", True), ("short", False)]

    def test_flush_reports_a_discarded_tail(self) -> None:
        # Over the limit with no terminating newline: the buffer is dropped and
        # flush must still report the truncation rather than staying silent.
        assembler = LineAssembler(max_line_bytes=16)
        assembler.feed(b"z" * 64)
        trailing = assembler.flush()
        assert trailing is not None
        assert trailing.truncated is True
        assert trailing.text == ""

    def test_event_from_truncated_line(self) -> None:
        assembler = LineAssembler(max_line_bytes=5)
        lines = assembler.feed(b"x" * 50 + b"\n")
        ev = event_from_line(lines[0])
        assert ev.type == "truncated_line"
        assert ev.level is Level.WARN

    def test_default_cap_is_ten_mib(self) -> None:
        assert DEFAULT_MAX_LINE_BYTES == 10 * 1024 * 1024
