"""A credential's *variable name* is not a credential.

``credential_env: OPENROUTER_API_KEY`` documents where a key lives; the value is an
identifier, not the secret.  Because the assignment pattern matched the label, the
all-caps identifier was judged a secret VALUE and ``prepare_capture`` rejected the
whole message (``plaintext_secret_rejected``) — twenty-five ordinary configuration
notes in one day never reached the store.

The exemption is deliberately narrow: it only fires when the label names an
environment variable AND the value is an UPPER_SNAKE identifier, so a real key typed
into that same field is still refused.
"""

import pytest

from scope_recall.core.secret_patterns import contains_secret_like_text

# label names a variable that HOLDS a credential; the value is that variable's name
ENV_NAME_VALUES = [
    "credential_env: OPENROUTER_API_KEY",
    '"credential_env": "STEPFUN_API_KEY"',
    "api_key_env=ZHIPU_API_KEY",
    "api_key_variable: DEEPSEEK_API_KEY",
    "token_env = GITHUB_TOKEN",
    "secret_variable: AWS_SECRET_ACCESS_KEY",
    "credential_env = NONE",
    "OPENROUTER_API_KEY",
    "39f4a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e",
]

# same label, but the value is a credential — the exemption must not swallow these
REAL_SECRETS = [
    "credential_env: sk-proj-1234567890abcdefghijklmn",
    "credential_env: Hunter2024",
    "sk-proj-1234567890abcdefghijklmn",
    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "password: Hunter2024x",
    "-----BEGIN RSA PRIVATE KEY-----",
]


@pytest.mark.parametrize("text", ENV_NAME_VALUES)
def test_env_name_value_is_not_treated_as_a_secret(text):
    assert contains_secret_like_text(text) is False


@pytest.mark.parametrize("text", REAL_SECRETS)
def test_real_credentials_are_still_detected(text):
    assert contains_secret_like_text(text) is True
