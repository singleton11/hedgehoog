from config import Settings


def test_production_http_requires_explicit_exception():
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret="s" * 48,
        admin_password="long-secure-password",
        cookie_secure=False,
        allow_insecure_http=True,
    )
    assert not settings.cookie_secure
    assert settings.allow_insecure_http
