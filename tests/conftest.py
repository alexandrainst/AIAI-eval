"""Global fixtures for unit tests."""

import pytest

from src.aiai_eval.config import EvaluationConfig


@pytest.fixture(scope="session")
def evaluation_config():
    yield EvaluationConfig(
        raise_error_on_invalid_model=True,
        cache_dir="cache_dir",
        use_auth_token=True,
        progress_bar=True,
        save_results=True,
        verbose=True,
        track_carbon_emissions=True,
        country_iso_code="DNK",
        measure_power_secs=5,
        testing=True,
    )
