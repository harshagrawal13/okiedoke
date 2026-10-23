from app.launch import load_env_file


def test_load_env_file_parses_export_and_quoted_values_without_overwriting(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text('X_CLIENT_ID=client\nexport X_CLIENT_SECRET="secret with spaces"\nSECRET_KEY=a=b=c\n')
    monkeypatch.setenv("X_CLIENT_ID", "already-set")

    load_env_file(path)

    import os
    assert os.environ["X_CLIENT_ID"] == "already-set"
    assert os.environ["X_CLIENT_SECRET"] == "secret with spaces"
    assert os.environ["SECRET_KEY"] == "a=b=c"
