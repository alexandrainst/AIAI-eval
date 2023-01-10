"""Utility functions related to the Leaderboard and associated REST API."""

import requests

from .task_configs import get_all_task_configs

TEST_URL = "http://fastapi.localhost:8008"

def get_task(task_name: str) -> dict:
    """Get a task from the Leaderboard.

    Args:
        task_name (str): The name of the task.

    Returns:
        task (dict): The task configuration.

    Raises:
        ValueError: If the task is not found.
    """
    # Check if task is valid
    try:
        task_config = get_all_task_configs()[task_name]
    except KeyError:
        raise ValueError(f"Task {task_name} not found.")

    # Get task from Leaderboard
    task = requests.get(
        f"{TEST_URL}/{task_config.name}"
    ).json()

    # Check if we got a valid response
    if task == {"error": "table not found"}:
        raise ValueError(f"Task {task_name} not found.")
    return task