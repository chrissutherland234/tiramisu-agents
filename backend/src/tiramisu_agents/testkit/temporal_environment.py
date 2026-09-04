"""Consistent Temporal time-skipping test-server startup."""

import os

from temporalio.testing import WorkflowEnvironment


async def start_time_skipping_environment() -> WorkflowEnvironment:
    """Start virtual time, optionally reusing an explicitly supplied local binary."""

    return await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.getenv("TEMPORAL_TEST_SERVER_PATH")
    )
