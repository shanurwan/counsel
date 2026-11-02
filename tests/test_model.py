"""The NVIDIA adapter's failure handling, with no network."""

import pytest

from counsel.model import ModelError, NvidiaModel, RecordingModel, ReplayModel

OK = {"choices": [{"message": {"content": " ok "}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
MSGS = [{"role": "user", "content": "hi"}]


def scripted(*responses):
    calls = []
    it = iter(responses)

    def transport(url, headers, body, timeout):
        calls.append(body["model"])
        return next(it)

    return transport, calls


def model(transport, models=("a", "b")):
    return NvidiaModel(api_key="k", models=models, transport=transport, sleep=lambda s: None,
                       min_interval_s=0)


def test_success_records_model_and_usage():
    transport, calls = scripted((200, OK))
    c = model(transport).complete(MSGS)
    assert (c.text, c.model, c.prompt_tokens, c.output_tokens) == ("ok", "a", 5, 1)


def test_retired_model_falls_through_to_the_next():
    transport, calls = scripted((410, {"title": "Gone"}), (200, OK))
    assert model(transport).complete(MSGS).model == "b"
    assert calls == ["a", "b"]


def test_rate_limit_and_server_errors_retry_with_backoff():
    transport, calls = scripted((429, {}), (503, {}), (200, OK))
    assert model(transport).complete(MSGS).model == "a"
    assert calls == ["a", "a", "a"]


def test_client_error_is_not_retried():
    transport, calls = scripted((400, {"detail": "bad request"}))
    with pytest.raises(ModelError, match="400"):
        model(transport).complete(MSGS)
    assert calls == ["a"]


def test_all_models_exhausted_raises():
    transport, _ = scripted((410, {}), (404, {}))
    with pytest.raises(ModelError, match="all models failed"):
        model(transport).complete(MSGS)


def test_replay_returns_recordings_and_refuses_unrecorded(tmp_path):
    transport, _ = scripted((200, OK))
    recorded = RecordingModel(model(transport), tmp_path).complete(MSGS)
    replayed = ReplayModel(tmp_path).complete(MSGS)
    assert (replayed.text, replayed.model) == (recorded.text, recorded.model)
    with pytest.raises(ModelError, match="no recording"):
        ReplayModel(tmp_path).complete([{"role": "user", "content": "something else"}])


def test_truncated_output_is_an_error_not_an_answer():
    truncated = {"choices": [{"message": {"content": "Core team: blocked on"}, "finish_reason": "length"}]}
    transport, _ = scripted((200, truncated))
    with pytest.raises(ModelError, match="truncated"):
        model(transport).complete(MSGS)


def test_thinking_is_disabled_so_it_cannot_eat_the_token_budget():
    seen = []

    def transport(url, headers, body, timeout):
        seen.append(body)
        return 200, OK

    model(transport).complete(MSGS)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
