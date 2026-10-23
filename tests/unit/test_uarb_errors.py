import re
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from uarb_agent.models import DocType, EmptyTab, LayoutChanged, MatterNotFound, SiteUnavailable, UnexpectedModal
from uarb_agent.uarb.errors import as_agent_error, clean_modal_text, modal_error

NOT_FOUND = "No Records Found\nNo records matched your search request. Click OK to return to Search page.\nOK"
EMPTY_TAB = "No Matching Records\nClick Ok to return to Matter\nOK"


def test_not_found_dialog():
    err = modal_error(NOT_FOUND, matter="M99999")
    assert isinstance(err, MatterNotFound) and err.matter == "M99999"


def test_empty_tab_dialog():
    err = modal_error(EMPTY_TAB, matter="M12205", tab=DocType.TRANSCRIPTS)
    assert isinstance(err, EmptyTab) and err.tab is DocType.TRANSCRIPTS


def test_empty_tab_dialog_without_tab_is_unexpected():
    assert isinstance(modal_error(EMPTY_TAB, matter="M12205"), UnexpectedModal)


def test_unknown_dialog_keeps_text():
    err = modal_error("Session expired\n\nPlease reload\nOK", matter="M12205")
    assert isinstance(err, UnexpectedModal)
    assert err.text == "Session expired | Please reload | OK"


def test_download_dialog_fixture_text_is_not_an_error_dialog():
    html = (Path(__file__).parents[1] / "fixtures" / "modal_0.html").read_text()
    text = clean_modal_text(re.sub(r"<[^>]+>", "\n", html))
    assert text.startswith("Download Files")
    assert isinstance(modal_error(text, matter="M12205"), UnexpectedModal)


def test_playwright_failures_become_site_unavailable():
    err = as_agent_error(PlaywrightTimeout("Timeout 15000ms exceeded.\n=== logs ==="))
    assert isinstance(err, SiteUnavailable)
    assert str(err) == "TimeoutError: Timeout 15000ms exceeded."
    assert isinstance(as_agent_error(PlaywrightError("net::ERR_NAME_NOT_RESOLVED")), SiteUnavailable)


def test_agent_errors_pass_through_and_others_are_not_mapped():
    err = LayoutChanged("x")
    assert as_agent_error(err) is err
    assert as_agent_error(ValueError("bug")) is None
