"""Command-line authoring workflow tests."""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest
from tiramisu_agents.projects.cli import check_project, describe_project, main, simulate_project


def test_startproject_creates_a_compilable_conventional_package(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "acme_service"

    assert main(("startproject", "acme_service", str(target))) == 0
    assert "Created acme_service" in capsys.readouterr().out
    assert (target / "README.md").is_file()
    assert (target / "src" / "acme_service" / "project.py").is_file()
    assert (target / "tests" / "test_project.py").is_file()

    source_path = str(target / "src")
    sys.path.insert(0, source_path)
    try:
        importlib.invalidate_caches()
        result = check_project("acme_service:create_project")
        description = describe_project("acme_service:create_project")
        simulation = asyncio.run(
            simulate_project("acme_service:create_project", scenario_id="happy_path")
        )
    finally:
        sys.path.remove(source_path)

    assert result.startswith("OK: Acme Service compiles")
    assert "Journey: Manage one piece of work" in description
    assert "[work.created]" in description
    assert 'Work status is "completed"' in description
    assert "Maximum journey lifetime: 90 days" in description
    assert "1. A new item starts the journey." in description
    assert simulation.startswith("PASS: Work is completed [happy_path]")


def test_startproject_refuses_to_overwrite_an_existing_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "valuable.txt").write_text("keep me", encoding="utf-8")

    assert main(("startproject", "existing", str(target))) == 2

    assert "not empty" in capsys.readouterr().err
    assert (target / "valuable.txt").read_text(encoding="utf-8") == "keep me"


def test_startproject_rejects_a_name_that_cannot_be_imported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "invalid"

    assert main(("startproject", "class", str(target))) == 2

    assert "snake_case" in capsys.readouterr().err
    assert not target.exists()


def test_describe_can_emit_machine_readable_compiled_metadata() -> None:
    document = json.loads(
        describe_project("tiramisu_agents.builtin:create_fictional_project", as_json=True)
    )

    assert document["id"] == "fictional_booking"
    assert document["journeys"][0]["id"] == "enquiry_to_booking"
    assert document["journeys"][0]["completion_requirements"] == {
        "booking.status": "confirmed",
        "calendar.status": "created",
        "payment.status": "completed",
    }
    assert document["journeys"][0]["communications"]["opt_out_event_types"] == [
        "customer.email_opted_out"
    ]
    assert document["journeys"][0]["limits"]["max_outbound_messages_per_window"] == 5


def test_describe_explains_customer_contact_rules_in_business_terms() -> None:
    description = describe_project("tiramisu_agents.builtin:create_fictional_project")

    assert "Customer-contact safety:" in description
    assert "Contact actions: send_message" in description
    assert "Genuine replies: customer.email_received" in description
    assert "Opt-outs: customer.email_opted_out" in description
    assert "Automated responses: customer.email_auto_replied" in description
    assert "Contact budget: 5 per 24 hours; 50 for the whole journey" in description


@pytest.mark.asyncio
async def test_simulate_runs_a_named_scenario_and_can_emit_json() -> None:
    rendered = await simulate_project(
        "tiramisu_agents.builtin:create_fictional_project",
        scenario_id="happy_path",
    )
    document = json.loads(
        await simulate_project(
            "tiramisu_agents.builtin:create_fictional_project",
            scenario_id="happy_path",
            as_json=True,
        )
    )

    assert rendered.startswith("PASS: A customer books and pays [happy_path]")
    assert "approval: Approved request_payment" in rendered
    assert document["passed"] is True
    assert document["final_status"] == "completed"
    assert document["action_types"] == [
        "find_available_slots",
        "send_message",
        "propose_booking",
        "request_payment",
        "create_calendar_event",
    ]
