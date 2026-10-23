"""Turn what the site shows (dialog text, Playwright failures) into the shared typed errors."""

from __future__ import annotations

from playwright.async_api import Error as PlaywrightError

from uarb_agent.models import AgentError, DocType, EmptyTab, MatterNotFound, SiteUnavailable, UnexpectedModal


def clean_modal_text(text: str) -> str:
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


def modal_error(text: str, *, matter: str, tab: DocType | None = None) -> AgentError:
    """Map a WebDirect dialog to an error. Search miss: 'No Records Found'; empty tab: 'No Matching Records'."""
    lowered = text.lower()
    if "no records found" in lowered or "no records matched" in lowered:
        return MatterNotFound(matter)
    if "no matching records" in lowered and tab is not None:
        return EmptyTab(matter, tab)
    return UnexpectedModal(clean_modal_text(text))


def as_agent_error(exc: BaseException) -> AgentError | None:
    """Expected errors pass through; Playwright timeouts and network failures mean the site is unavailable."""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, PlaywrightError):  # TimeoutError subclasses Error
        msg = str(exc).strip()
        return SiteUnavailable(f"{type(exc).__name__}: {msg.splitlines()[0] if msg else 'no message'}")
    return None
