"""Phase D — browser UI renders real events (headless Chromium via Playwright)."""

import pathlib

import pytest

from conftest import UI, audit_records

ART = pathlib.Path(__file__).parent / "artifacts"
TIMEOUT = 300_000


@pytest.fixture
def page(llm_ready):
    from playwright.sync_api import sync_playwright
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        pg.goto(UI)
        pg.wait_for_selector("[data-user].active")
        yield pg
        browser.close()


def select_user(page, user):
    page.click(f"[data-user={user}]")
    page.wait_for_selector(f"[data-user={user}].active")
    page.wait_for_function("() => document.querySelector('[data-testid=transcript]').innerText.includes('Pick an objective')")


def test_d1_task_drives_chat_and_system_view(page):
    select_user(page, "alice")
    page.click("[data-objective=clean]")
    page.wait_for_selector("[data-testid=plan]", timeout=TIMEOUT)
    page.wait_for_selector("[data-testid=agent-message]", timeout=TIMEOUT)
    # The system view animates the same real requests that appear in the chat and the broker's log.
    ids_in_chat = page.eval_on_selector_all(".req-line", "els => els.map(e => e.dataset.requestId)")
    assert ids_in_chat
    page.wait_for_function(f"() => document.querySelectorAll('[data-testid=history] .chip').length === {len(ids_in_chat)}", timeout=60_000)
    logged = {r["request_id"] for r in audit_records()}
    assert set(ids_in_chat) <= logged
    page.screenshot(path=str(ART / "d1_alice_clean.png"))


def test_d2_denied_request_animates_red_with_reason(page):
    select_user(page, "bob")
    page.click("[data-objective=compensation]")
    verdict = page.wait_for_selector("[data-testid=verdict][data-result=deny]", timeout=TIMEOUT)
    assert page.inner_text("[data-testid=verdict] [data-testid=rule-fired]") == "user_rights"
    assert "bob" in verdict.inner_text() and "employees" in verdict.inner_text()
    assert page.get_attribute("[data-stage=user_rights]", "class").endswith("fail")
    assert "denied" in page.get_attribute("[data-testid=broker-node]", "class")
    page.screenshot(path=str(ART / "d2_bob_denied.png"))
    page.wait_for_selector("[data-testid=agent-message]", timeout=TIMEOUT)


def test_d3_audit_log_updates_live(page):
    select_user(page, "carol")
    before = page.locator("[data-testid=audit-log] tbody tr[data-request-id]").count()
    page.click("[data-objective=clean]")
    page.wait_for_selector("[data-testid=agent-message]", timeout=TIMEOUT)
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-testid=audit-log] tbody tr[data-request-id]').length > {before}", timeout=10_000
    )
    newest = page.get_attribute("[data-testid=audit-log] tbody tr[data-request-id]", "data-request-id")
    assert newest in {r["request_id"] for r in audit_records()}
    assert "carol" in page.inner_text("[data-testid=audit-log] tbody tr:first-child")
    page.screenshot(path=str(ART / "d3_audit.png"))
