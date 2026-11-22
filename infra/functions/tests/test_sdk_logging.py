import logging

import function_app  # noqa: F401  (the import sets the level)


def test_azure_sdk_request_logging_is_quiet():
    """Request URLs carry blob paths with competition and file names: INFO must not reach the logs."""
    logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
    assert logger.level == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)
