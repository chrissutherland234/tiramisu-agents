"""Backward-determinism checks against committed Temporal histories."""

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from tiramisu_agents.temporal.workflows.mailbox import ProcessMailboxWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_id", "fixture_name"),
    (
        ("mailbox-replay-v1", "mailbox_v1.json"),
        ("mailbox-continue-replay-v1", "mailbox_continue_as_new_v1.json"),
        ("mailbox-manual-wake-replay-v1", "mailbox_manual_wake_v1.json"),
    ),
)
async def test_mailbox_v1_histories_replay_with_current_workflow(
    workflow_id: str, fixture_name: str
) -> None:
    fixture = Path(__file__).with_name(fixture_name)
    history = WorkflowHistory.from_json(workflow_id, fixture.read_text(encoding="utf-8"))

    result = await Replayer(workflows=[ProcessMailboxWorkflow]).replay_workflow(history)

    assert result.replay_failure is None
