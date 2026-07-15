from mcg.token.oauth import DEFAULT_SCOPE


def test_default_scope_has_substrate():
    assert "substrate.office.com" in DEFAULT_SCOPE
    assert "offline_access" in DEFAULT_SCOPE
