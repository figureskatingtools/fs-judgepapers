"""The Azure SDK's per-request log lines (storage URLs with blob paths and
table keys) stay out of the logs."""
import logging

import function_app as fa


def test_the_sdk_http_logging_policy_logs_warnings_only():
    logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
    assert fa.HTTP_LOGGING_POLICY_LOGGER == logger.name
    assert logger.level == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)
