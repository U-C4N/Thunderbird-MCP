"""The pure parts of `tools/smoke_search.py`.

The script itself needs a live Thunderbird, but everything it decides before it
asks a question — which folder is worth searching, which word to search for, what
a failed call means, whether a cursor walk lost a page — is ordinary code, and it
is the part that silently ruins an acceptance run when it is wrong.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script():
    """Import `tools/smoke_search.py`, which is a script and not a package module."""
    spec = importlib.util.spec_from_file_location(
        "smoke_search", ROOT / "tools" / "smoke_search.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_script()


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeResult:
    """What `mcp.Client.call_tool` hands back, minus everything unused here."""

    def __init__(
        self,
        *,
        is_error: bool = False,
        text: str = "",
        structured: dict[str, Any] | None = None,
    ) -> None:
        self.is_error = is_error
        self.content = [FakeBlock(text)] if text else []
        self.structured_content = structured if structured is not None else {}


# --------------------------------------------------------------- corpus discovery

FOLDERS = [
    {"id": "account1://", "name": "Local Folders", "isRoot": True},
    {"id": "account1://Inbox", "name": "Inbox", "totalMessageCount": 195},
    {"id": "account1://Archive", "name": "Archive", "totalMessageCount": 40},
    {"id": "account2://Junk", "name": "Junk", "totalMessageCount": 4},
    {"id": "account1://Saved", "name": "Saved", "isVirtual": True, "totalMessageCount": 900},
]


def test_biggest_folder_takes_the_fullest_real_folder():
    folder = smoke.biggest_folder(FOLDERS)
    assert folder is not None
    assert folder["id"] == "account1://Inbox"


def test_biggest_folder_skips_a_saved_search():
    """A virtual folder counts messages it does not hold, and cannot be paged."""
    assert smoke.biggest_folder(FOLDERS)["id"] != "account1://Saved"


def test_biggest_folder_gives_up_when_nothing_is_big_enough():
    small = [{"id": "account1://Inbox", "totalMessageCount": 19}]
    assert smoke.biggest_folder(small) is None
    assert smoke.biggest_folder([{"id": "account1://Inbox"}]) is None


MESSAGES = [
    {"id": 1, "subject": "Re: teklif dosyası"},
    {"id": 2, "subject": "Fwd: Teklif revizyonu"},
    {"id": 3, "subject": "teklif — son hali"},
    {"id": 4, "subject": "teklif kabul edildi"},
    {"id": 5, "subject": "yeni bir konu"},
]


def test_common_term_picks_the_word_the_most_subjects_share():
    assert smoke.common_term(MESSAGES) == "teklif"


def test_common_term_keeps_the_spelling_it_saw():
    """Case is part of the term: the live check searches for exactly this string."""
    shouty = [{"subject": f"WEEKLY report {n}"} for n in range(3)]
    assert smoke.common_term(shouty) == "WEEKLY"


def test_common_term_ignores_short_words_and_rare_ones():
    thin = [
        {"subject": "one two invoice"},
        {"subject": "one two receipt"},
        {"subject": "one two summary"},
    ]
    # "one"/"two" are under four letters; nothing else reaches three subjects.
    assert smoke.common_term(thin) is None


def test_common_term_counts_subjects_not_repeats():
    """A word said four times in one subject is still one subject."""
    repeated = [{"subject": "invoice invoice invoice invoice"}, {"subject": "invoice again"}]
    assert smoke.common_term(repeated) is None


# ------------------------------------------------------------------- check helper


def test_check_turns_a_tool_error_into_a_failure_carrying_its_text(capsys):
    report = smoke.Report()
    payload = report.check("mail_search subject", FakeResult(is_error=True, text="no such folder"))
    assert payload is None
    assert report.failures == ["mail_search subject"]
    printed = capsys.readouterr().out
    assert "FAIL" in printed
    assert "no such folder" in printed


def test_check_returns_the_payload_when_the_call_worked(capsys):
    report = smoke.Report()
    payload = report.check("mail_search subject", FakeResult(structured={"count": 5}), "5 hits")
    assert payload == {"count": 5}
    assert report.failures == []
    assert "PASS" in capsys.readouterr().out


def test_check_fails_when_the_verdict_says_so(capsys):
    report = smoke.Report()
    result = FakeResult(structured={"count": 0})
    payload = report.check("mail_search subject", result, verify=lambda got: "nothing matched")
    assert payload is None
    assert report.failures == ["mail_search subject"]
    assert "nothing matched" in capsys.readouterr().out


def test_an_error_with_no_text_still_says_something(capsys):
    report = smoke.Report()
    report.check("search_global ranked", FakeResult(is_error=True))
    assert report.failures == ["search_global ranked"]
    assert capsys.readouterr().out.strip() != ""


def test_a_skip_is_not_a_failure(capsys):
    report = smoke.Report()
    report.skip("search_global ranked", "the index has no hits for this term")
    assert report.failures == []
    assert "SKIP" in capsys.readouterr().out


# ------------------------------------------------------------------ paging checks


def test_paging_gap_is_silent_when_the_walk_matches_one_page():
    assert smoke.paging_gap([1, 2, 3, 4], [1, 2, 3, 4]) is None


def test_paging_gap_names_a_skipped_page():
    """The 1.2.0 bug: a limit that stopped mid-page dropped the rest of it."""
    walked = [1, 2, 3, 101, 102, 103]
    straight = list(range(1, 13))
    problem = smoke.paging_gap(walked, straight)
    assert problem is not None
    assert "9 missing" in problem


def test_paging_gap_reports_ids_the_walk_invented():
    problem = smoke.paging_gap([1, 2, 99], [1, 2, 3])
    assert problem is not None
    assert "99" in problem


def test_paging_gap_notices_reordering():
    problem = smoke.paging_gap([2, 1], [1, 2])
    assert problem is not None
    assert "order" in problem


# ----------------------------------------------------------------- folder id sanity


def test_invalid_folder_ids_accepts_ids_the_profile_knows():
    items = [{"folderId": "account1://Inbox"}, {"folderId": "account1://Archive"}]
    assert smoke.invalid_folder_ids(items, {"account1://Inbox", "account1://Archive"}) == []


def test_invalid_folder_ids_reports_anything_unrecognisable():
    items = [{"folderId": None}, {"folderId": "Inbox"}, {"folderId": "account9://Nope"}]
    bad = smoke.invalid_folder_ids(items, {"account1://Inbox"})
    assert len(bad) == 3
    assert any("account9://Nope" in entry for entry in bad)


def test_invalid_folder_ids_falls_back_to_shape_without_a_folder_list():
    items = [{"folderId": "account9://Nope"}]
    assert smoke.invalid_folder_ids(items, None) == []


@pytest.mark.parametrize("dates", [["2026-01-01", "2026-02-01"], [], ["2026-01-01"]])
def test_ascending_accepts_ordered_dates(dates):
    assert smoke.out_of_order(dates) is None


def test_ascending_reports_the_pair_that_went_backwards():
    problem = smoke.out_of_order(["2026-02-01", "2026-01-01"])
    assert problem is not None
    assert "2026-01-01" in problem
