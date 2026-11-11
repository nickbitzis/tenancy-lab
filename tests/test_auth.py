import pytest

from tenancylab.auth import AuthError, issue_token, resolve, tenant_from_host, verify_token

S, D = "test-secret", "tenancylab.test"


def test_token_roundtrip_and_tamper():
    tok = issue_token(S, "acme", "usr_1", "admin")
    assert verify_token(S, tok)["tid"] == "acme"
    with pytest.raises(AuthError):
        verify_token("other-secret", tok)
    with pytest.raises(AuthError):
        verify_token(S, tok + "x")


def test_expired_token():
    with pytest.raises(AuthError, match="expired"):
        verify_token(S, issue_token(S, "acme", "u", ttl=-1))


def test_subdomain_parsing():
    assert tenant_from_host("acme.tenancylab.test:8875", D) == "acme"
    assert tenant_from_host("api.tenancylab.test", D) is None
    assert tenant_from_host("a.b.tenancylab.test", D) is None
    assert tenant_from_host("evil-tenancylab.test", D) is None


@pytest.mark.parametrize(
    "header,host,status",
    [
        ("bolt", None, 403),
        (None, "bolt.tenancylab.test", 403),
        ("acme", "bolt.tenancylab.test", 400),
        (None, None, 400),
    ],
)
def test_resolution_failures(header, host, status):
    tok = "Bearer " + issue_token(S, "acme", "u")
    with pytest.raises(AuthError) as exc:
        resolve(tok, header, host, S, D)
    assert exc.value.status == status


def test_resolution_success():
    tok = "Bearer " + issue_token(S, "acme", "u")
    assert resolve(tok, None, "acme.tenancylab.test", S, D)[0] == "acme"
    assert resolve(tok, "acme", None, S, D)[0] == "acme"
