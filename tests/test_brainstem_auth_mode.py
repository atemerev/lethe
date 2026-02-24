from lethe.actor import ActorConfig, ActorRegistry
from lethe.actor.brainstem import Brainstem
from lethe.config import Settings


def test_brainstem_collects_openai_oauth_auth_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    workspace = tmp_path / "workspace"
    memory = tmp_path / "memory"
    config_dir = tmp_path / "config"
    db_parent = tmp_path / "data"
    workspace.mkdir()
    memory.mkdir()
    config_dir.mkdir()
    db_parent.mkdir()

    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_ids="1",
        workspace_dir=workspace,
        memory_dir=memory,
        lethe_config_dir=config_dir,
        db_path=db_parent / "lethe.db",
    )

    registry = ActorRegistry()
    cortex = registry.spawn(
        ActorConfig(name="cortex", group="main", goals="serve"),
        is_principal=True,
    )
    brainstem = Brainstem(
        registry=registry,
        settings=settings,
        cortex_id=cortex.id,
        install_dir=str(tmp_path),
    )

    snapshot = brainstem._collect_resource_snapshot()
    assert snapshot["auth_mode"] == "openai_subscription_oauth"
