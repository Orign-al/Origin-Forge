def test_cli_identity_is_non_secret_compatible_and_not_cached(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/api/v1/cli/identity")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "service": "H100 Portal",
        "api_compatibility": "h100.cli.v1",
        "cli_versions": ["1.0.0"],
    }
    assert "token" not in response.text.lower()
