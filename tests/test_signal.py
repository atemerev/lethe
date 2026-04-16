"""Tests for Signal integration."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from lethe.signal.client import SignalClient, SignalClientError
from lethe.signal import SignalBot, SignalBotAdapter
from lethe.proxy_bot import MockMessage


class TestSignalClient:
    """Tests for the signal-cli HTTP client."""

    @pytest.mark.asyncio
    async def test_rpc_formats_jsonrpc_request(self):
        """RPC calls should use JSON-RPC 2.0 format."""
        client = SignalClient(base_url="http://localhost:8080", account="+15551234567")
        client._http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value ={"jsonrpc": "2.0", "result": {"timestamp": 123}, "id": "1"}
        mock_resp.raise_for_status = MagicMock()
        client._http.post = AsyncMock(return_value=mock_resp)

        result = await client.rpc("send", {"message": "hello", "recipient": ["+15559999999"]})

        assert result == {"timestamp": 123}
        call_args = client._http.post.call_args
        assert call_args[0][0] == "/api/v1/rpc"
        payload = call_args[1]["json"]
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "send"
        assert payload["params"]["message"] == "hello"

    @pytest.mark.asyncio
    async def test_rpc_raises_on_error(self):
        """RPC errors should raise SignalClientError."""
        client = SignalClient()
        client._http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value ={
            "jsonrpc": "2.0",
            "error": {"code": -1, "message": "Unknown recipient"},
            "id": "1",
        }
        mock_resp.raise_for_status = MagicMock()
        client._http.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(SignalClientError) as exc_info:
            await client.rpc("send", {})
        assert exc_info.value.code == -1
        assert "Unknown recipient" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_includes_account(self):
        """send() should include account in params."""
        client = SignalClient(account="+15551234567")
        client._http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value ={"jsonrpc": "2.0", "result": {"timestamp": 100}, "id": "1"}
        mock_resp.raise_for_status = MagicMock()
        client._http.post = AsyncMock(return_value=mock_resp)

        await client.send(recipient="+15559999999", message="test")

        payload = client._http.post.call_args[1]["json"]
        assert payload["params"]["account"] == "+15551234567"
        assert payload["params"]["recipient"] == ["+15559999999"]
        assert payload["params"]["message"] == "test"

    @pytest.mark.asyncio
    async def test_send_with_attachments(self):
        """send() should include attachment paths."""
        client = SignalClient(account="+1555")
        client._http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value ={"jsonrpc": "2.0", "result": {}, "id": "1"}
        mock_resp.raise_for_status = MagicMock()
        client._http.post = AsyncMock(return_value=mock_resp)

        await client.send("+1999", "caption", attachments=["/tmp/photo.jpg"])

        payload = client._http.post.call_args[1]["json"]
        assert payload["params"]["attachment"] == ["/tmp/photo.jpg"]

    @pytest.mark.asyncio
    async def test_send_reaction(self):
        """send_reaction() should format params correctly."""
        client = SignalClient(account="+1555")
        client._http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value ={"jsonrpc": "2.0", "result": {}, "id": "1"}
        mock_resp.raise_for_status = MagicMock()
        client._http.post = AsyncMock(return_value=mock_resp)

        await client.send_reaction("+1999", "👍", "+1999", 12345)

        payload = client._http.post.call_args[1]["json"]
        assert payload["method"] == "sendReaction"
        assert payload["params"]["emoji"] == "\U0001f44d"
        assert payload["params"]["target-timestamp"] == 12345

    @pytest.mark.asyncio
    async def test_not_started_raises(self):
        """RPC should raise if client not started."""
        client = SignalClient()
        with pytest.raises(RuntimeError, match="not started"):
            await client.rpc("send", {})


class TestSignalBotAdapter:
    """Tests for the duck-type adapter."""

    @pytest.mark.asyncio
    async def test_send_message_returns_mock(self):
        """Adapter should return MockMessage with timestamp."""
        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value={"timestamp": 42})
        adapter = SignalBotAdapter(mock_client)

        result = await adapter.send_message("+15551234567", "hello")

        assert isinstance(result, MockMessage)
        assert result.message_id == 42
        mock_client.send.assert_called_once_with(recipient="+15551234567", message="hello")

    @pytest.mark.asyncio
    async def test_send_photo_uses_attachment(self):
        """Photos should be sent as attachments."""
        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value={"timestamp": 1})
        adapter = SignalBotAdapter(mock_client)

        await adapter.send_photo("+1555", "/tmp/photo.jpg", caption="Look!")

        mock_client.send.assert_called_once_with(
            recipient="+1555", message="Look!", attachments=["/tmp/photo.jpg"]
        )

    @pytest.mark.asyncio
    async def test_send_chat_action_best_effort(self):
        """Typing indicator should not raise on failure."""
        mock_client = AsyncMock()
        mock_client.send_typing = AsyncMock(side_effect=Exception("not supported"))
        adapter = SignalBotAdapter(mock_client)

        # Should not raise
        await adapter.send_chat_action("+1555", "typing")


class TestSignalBot:
    """Tests for the SignalBot class."""

    def _make_bot(self):
        """Create a SignalBot with mock settings."""
        settings = MagicMock()
        settings.signal_cli_url = "http://localhost:8080"
        settings.signal_account = "+15551234567"
        settings.signal_allowed_number_list = ["+15551234567"]
        settings.workspace_dir = "/tmp"
        conv_mgr = MagicMock()
        bot = SignalBot(settings=settings, conversation_manager=conv_mgr)
        bot.client = AsyncMock()
        return bot

    def test_authorize_own_account(self):
        """Own account (Note to Self) should always be authorized."""
        bot = self._make_bot()
        assert bot._is_authorized("+15551234567")

    def test_authorize_allowed_number(self):
        """Numbers in allowlist should be authorized."""
        bot = self._make_bot()
        bot.settings.signal_allowed_number_list = ["+15559999999"]
        assert bot._is_authorized("+15559999999")

    def test_reject_unauthorized(self):
        """Numbers not in allowlist should be rejected."""
        bot = self._make_bot()
        bot.settings.signal_allowed_number_list = ["+15559999999"]
        assert not bot._is_authorized("+15550000000")

    def test_deny_all_when_empty(self):
        """Empty allowlist should only allow self (Note to Self)."""
        bot = self._make_bot()
        bot.settings.signal_allowed_number_list = []
        assert not bot._is_authorized("+15550000000")
        assert bot._is_authorized("+15551234567")  # own account still allowed

    @pytest.mark.asyncio
    async def test_handle_command_status(self):
        """Should handle /status command."""
        bot = self._make_bot()
        bot.conversation_manager.is_processing = MagicMock(return_value=False)
        bot.conversation_manager.is_debouncing = MagicMock(return_value=False)
        bot.conversation_manager.get_pending_count = MagicMock(return_value=0)
        bot.client.send = AsyncMock(return_value={"timestamp": 1})

        handled = await bot._handle_command("+1555", "/status", "/status")
        assert handled
        bot.client.send.assert_called()

    @pytest.mark.asyncio
    async def test_handle_command_unknown(self):
        """Unknown commands should return False."""
        bot = self._make_bot()
        handled = await bot._handle_command("+1555", "/unknown", "/unknown")
        assert not handled

    @pytest.mark.asyncio
    async def test_handle_event_text_message(self):
        """Text messages should be passed to conversation manager."""
        bot = self._make_bot()
        bot.process_callback = AsyncMock()
        bot.conversation_manager.add_message = AsyncMock()

        event = {
            "envelope": {
                "source": "+15551234567",
                "dataMessage": {
                    "timestamp": 12345,
                    "message": "Hello Lethe!",
                },
            }
        }
        await bot._handle_event(event)

        bot.conversation_manager.add_message.assert_called_once()
        call_kwargs = bot.conversation_manager.add_message.call_args[1]
        assert call_kwargs["chat_id"] == "+15551234567"
        assert call_kwargs["content"] == "Hello Lethe!"

    @pytest.mark.asyncio
    async def test_handle_event_command(self):
        """Commands should be handled, not passed to conversation manager."""
        bot = self._make_bot()
        bot.process_callback = AsyncMock()
        bot.conversation_manager.add_message = AsyncMock()
        bot.conversation_manager.is_processing = MagicMock(return_value=False)
        bot.conversation_manager.is_debouncing = MagicMock(return_value=False)
        bot.conversation_manager.get_pending_count = MagicMock(return_value=0)
        bot.client.send = AsyncMock(return_value={"timestamp": 1})

        event = {
            "envelope": {
                "source": "+15551234567",
                "dataMessage": {
                    "timestamp": 12345,
                    "message": "/status",
                },
            }
        }
        await bot._handle_event(event)

        bot.conversation_manager.add_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_unauthorized(self):
        """Unauthorized senders should be ignored."""
        bot = self._make_bot()
        bot.settings.signal_allowed_number_list = ["+15559999999"]
        bot.settings.signal_account = "+15559999999"
        bot.conversation_manager.add_message = AsyncMock()

        event = {
            "envelope": {
                "source": "+15550000000",
                "dataMessage": {
                    "timestamp": 12345,
                    "message": "sneaky",
                },
            }
        }
        await bot._handle_event(event)
        bot.conversation_manager.add_message.assert_not_called()


class TestSignalBotModelPicker:
    """Tests for the text-based model selection."""

    def _make_bot(self):
        settings = MagicMock()
        settings.signal_cli_url = "http://localhost:8080"
        settings.signal_account = "+15551234567"
        settings.signal_allowed_number_list = []
        bot = SignalBot(settings=settings)
        bot.client = AsyncMock()
        bot.client.send = AsyncMock(return_value={"timestamp": 1})
        return bot

    @pytest.mark.asyncio
    async def test_selection_stores_pending_state(self):
        """Model picker should store pending selection."""
        bot = self._make_bot()
        bot.agent = MagicMock()
        bot.agent.llm.config.model = "test-model"

        with patch("lethe.signal.get_available_providers") as mock_providers:
            with patch("lethe.signal.MODEL_CATALOG", {"test": {"main": [("Test", "test-model", "$1")]}}):
                mock_providers.return_value = [{"provider": "test", "label": "Test", "auth": "API"}]
                await bot._show_model_picker("+1555", "main")

        assert "+1555" in bot._pending_selection

    @pytest.mark.asyncio
    async def test_selection_applies_choice(self):
        """Selecting a number should switch the model."""
        bot = self._make_bot()
        bot.agent = MagicMock()
        bot.agent.llm.config.model = "old-model"
        bot.agent.llm.config.provider = "test"

        bot._pending_selection["+1555"] = ("main", [
            ("Model A", "model-a", "$1", "API"),
            ("Model B", "model-b", "$2", "API"),
        ])

        with patch("lethe.signal.provider_for_model", return_value=None):
            await bot._handle_selection("+1555", 2)

        assert bot.agent.llm.config.model == "model-b"
        assert "+1555" not in bot._pending_selection

    @pytest.mark.asyncio
    async def test_selection_invalid_choice(self):
        """Invalid number should keep pending selection."""
        bot = self._make_bot()
        bot._pending_selection["+1555"] = ("main", [("A", "a", "$1", "API")])

        await bot._handle_selection("+1555", 5)

        assert "+1555" in bot._pending_selection  # Still pending
