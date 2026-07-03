"""Tests for the IAM auth middleware's pure verification + session helpers.

The JWT path is exercised end to end with a locally generated RSA keypair:
sign an access token like Hanzo IAM would, then verify it through the same
code the middleware uses. Covers signature, exp, iss, aud and the org claim.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from middleware import iam_auth_middleware as m

ISS = "https://hanzo.id"
AUD = "hanzo-studio"


@pytest.fixture(scope="module")
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _token(priv, **overrides):
    now = int(time.time())
    claims = {
        "sub": "hanzo/alice",
        "iss": ISS,
        "aud": AUD,
        "exp": now + 300,
        "iat": now,
        "owner": "acme",
        "name": "Alice",
        "email": "alice@acme.com",
        "picture": "https://cdn/x.png",
    }
    claims.update(overrides)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "test"})


def test_valid_token_extracts_org(keys):
    priv, pub = keys
    user = m.verify_jwt(_token(priv), pub, ISS, AUD)
    assert user is not None
    assert user["org_id"] == "acme"
    assert user["sub"] == "hanzo/alice"
    assert user["name"] == "Alice"
    assert user["email"] == "alice@acme.com"
    assert user["avatar"] == "https://cdn/x.png"


def test_organization_claim_fallback(keys):
    priv, pub = keys
    tok = _token(priv, owner=None, organization="globex")
    assert m.verify_jwt(tok, pub, ISS, AUD)["org_id"] == "globex"


def test_org_defaults_when_absent(keys):
    priv, pub = keys
    tok = _token(priv, owner=None, organization=None)
    assert m.verify_jwt(tok, pub, ISS, AUD)["org_id"] == "default"


def test_preferred_username_fallback(keys):
    priv, pub = keys
    tok = _token(priv, name=None, preferred_username="alice")
    assert m.verify_jwt(tok, pub, ISS, AUD)["name"] == "alice"


def test_wrong_audience_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, aud="someone-else"), pub, ISS, AUD) is None


def test_wrong_issuer_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, iss="https://evil.example"), pub, ISS, AUD) is None


def test_expired_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, exp=int(time.time()) - 10), pub, ISS, AUD) is None


def test_missing_exp_rejected(keys):
    priv, pub = keys
    assert m.verify_jwt(_token(priv, exp=None), pub, ISS, AUD) is None


def test_bad_signature_rejected(keys):
    priv, _pub = keys
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    assert m.verify_jwt(_token(priv), other, ISS, AUD) is None


def test_aud_array_form_accepted(keys):
    """golang-jwt serializes a single audience as a string but multi as array;
    accept both."""
    priv, pub = keys
    assert m.verify_jwt(_token(priv, aud=[AUD, "other"]), pub, ISS, AUD)["org_id"] == "acme"


# --- signed OAuth state (CSRF) ---

def test_state_roundtrip():
    payload = {"n": "nonce", "v": "verifier", "t": "/workflows", "exp": int(time.time()) + 60}
    blob = m._sign_state(payload, "secret")
    got = m._verify_state(blob, "secret")
    assert got["n"] == "nonce" and got["t"] == "/workflows"


def test_state_tamper_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) + 60}, "secret")
    body, mac = blob.split(".", 1)
    assert m._verify_state(f"{body}x.{mac}", "secret") is None


def test_state_wrong_secret_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) + 60}, "secret")
    assert m._verify_state(blob, "other") is None


def test_state_expired_rejected():
    blob = m._sign_state({"n": "n", "exp": int(time.time()) - 1}, "secret")
    assert m._verify_state(blob, "secret") is None


# --- path exemptions ---

@pytest.mark.parametrize("path", [
    "/", "/callback", "/health", "/api/features",
    "/assets/x.js", "/main.css", "/logo.svg",
    "/v1/workers/register", "/v1/worker/execute",
])
def test_public_paths(path):
    assert m._is_public_path(path) is True


@pytest.mark.parametrize("path", ["/prompt", "/api/prompt", "/userdata/x", "/v1/compute/config"])
def test_protected_paths(path):
    assert m._is_public_path(path) is False
