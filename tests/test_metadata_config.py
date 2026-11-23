import pytest

from table_column_same_graph import config


def test_mcp_recovery_defaults_and_overrides(monkeypatch):
    for name in (
        "MCP_PAGE_RETRIES", "MCP_RETRY_BACKOFF_SECONDS",
        "MCP_SNAPSHOT_RESTARTS", "MCP_SNAPSHOT_EXPIRED_RPC_CODE",
    ):
        monkeypatch.delenv(name, raising=False)
    default = config.MCPConfig.from_env()
    assert (default.page_retries, default.retry_backoff_seconds) == (2, 1.0)
    assert (default.snapshot_restarts, default.snapshot_expired_rpc_code) == (1, None)

    monkeypatch.setenv("MCP_PAGE_RETRIES", "1")
    monkeypatch.setenv("MCP_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("MCP_SNAPSHOT_RESTARTS", "0")
    monkeypatch.setenv("MCP_SNAPSHOT_EXPIRED_RPC_CODE", "7401")  # Test platform only.
    override = config.MCPConfig.from_env()
    assert (override.page_retries, override.retry_backoff_seconds) == (1, 0)
    assert (override.snapshot_restarts, override.snapshot_expired_rpc_code) == (0, 7401)


@pytest.mark.parametrize(("name", "value"), [
    ("MCP_PAGE_RETRIES", "-1"), ("MCP_RETRY_BACKOFF_SECONDS", "nan"),
    ("MCP_RETRY_BACKOFF_SECONDS", "-1"), ("MCP_SNAPSHOT_RESTARTS", "-1"),
    ("MCP_SNAPSHOT_EXPIRED_RPC_CODE", "-32602"), ("MCP_SNAPSHOT_EXPIRED_RPC_CODE", "-32001"),
    ("MCP_TIMEOUT_SECONDS", "inf"), ("MCP_PAGE_SIZE", "0"),
])
def test_invalid_mcp_settings_fail_at_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.MCPConfig.from_env()


def test_quality_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("METADATA_QUALITY_MAX_DROP_FRACTION", raising=False)
    monkeypatch.delenv("METADATA_QUALITY_HISTORY_PATH", raising=False)
    default = config.MetadataQualityConfig.from_env()
    assert default.window_size == 7
    assert default.max_drop_fraction == 0.30
    assert default.history_path == config.PROJECT_ROOT / "metadata_quality" / "history.json"
    history = tmp_path / "quality.json"
    monkeypatch.setenv("METADATA_QUALITY_HISTORY_PATH", str(history))
    monkeypatch.setenv("METADATA_QUALITY_MAX_DROP_FRACTION", "0.15")
    custom = config.MetadataQualityConfig.from_env()
    assert custom.history_path == history
    assert custom.max_drop_fraction == 0.15


@pytest.mark.parametrize("threshold", ["-0.1", "1.1", "nan"])
def test_invalid_drop_threshold(monkeypatch, threshold):
    monkeypatch.setenv("METADATA_QUALITY_MAX_DROP_FRACTION", threshold)
    with pytest.raises(ValueError, match="METADATA_QUALITY_MAX_DROP_FRACTION"):
        config.MetadataQualityConfig.from_env()


@pytest.mark.parametrize("path", [" ", "memory/similarity_memory.jsonl"])
def test_independent_quality_history_path(monkeypatch, path):
    monkeypatch.setenv("METADATA_QUALITY_HISTORY_PATH", path)
    with pytest.raises(ValueError, match="METADATA_QUALITY_HISTORY_PATH"):
        config.MetadataQualityConfig.from_env()


@pytest.mark.parametrize(("settings", "name"), [
    (config.MCPConfig, "MCP_PAGE_RETRIES"),
    (config.MCPConfig, "MCP_SNAPSHOT_EXPIRED_RPC_CODE"),
    (config.MetadataQualityConfig, "METADATA_QUALITY_MAX_DROP_FRACTION"),
])
def test_malformed_numeric_settings_identify_the_variable(monkeypatch, settings, name):
    monkeypatch.setenv(name, "invalid-setting")
    with pytest.raises(ValueError, match=name) as error:
        settings.from_env()
    assert "invalid-setting" not in str(error.value)
