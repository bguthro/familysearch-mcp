"""Fixtures for the live beta integration suite.

Without a token (FS_BETA_TOKEN, or FS_USE_BRAVE=1) the whole suite skips.
"""
import os

import pytest

from familysearch import Discovery, Environment, FSClient, extract_brave_token


def token_available() -> bool:
    return bool(os.environ.get("FS_BETA_TOKEN")) or os.environ.get("FS_USE_BRAVE") == "1"


def _get_token() -> str:
    token = os.environ.get("FS_BETA_TOKEN")
    if token:
        return token
    return extract_brave_token()


@pytest.fixture(scope="session")
def beta_client():
    if not token_available():
        pytest.skip("no FS_BETA_TOKEN / FS_USE_BRAVE=1; skipping beta integration")
    return FSClient(_get_token(), Environment.BETA)


@pytest.fixture(scope="session")
def discovery(beta_client):
    return Discovery(beta_client)
