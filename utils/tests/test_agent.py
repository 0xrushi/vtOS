"""The vtOS chat agent (bin/agent.py) against a scripted fake model server."""

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class FakeRaw:
    def __init__(self, lines):
        self.stream = io.BytesIO(b"".join(lines))

    def readline(self):
        return self.stream.readline()


class FakeResponse:
    def __init__(self, lines, status=200, text=""):
        self.status_code = status
        self.raw = FakeRaw(lines)
        self.text = text
        self.closed = False

    def close(self):
        self.closed = True


def sse(*events):
    lines = [b"data: " + json.dumps(e).encode() + b"\n\n" for e in events]
    lines.append(b"data: [DONE]\n\n")
    return lines


def text_chunks(*pieces):
    return sse(*[{"choices": [{"delta": {"content": p}}]} for p in pieces])


def tool_call(call_id, name, args):
    text = json.dumps(args)
    half = len(text) // 2  # arguments arrive in pieces
    return sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id,
                                                  "function": {"name": name, "arguments": text[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": text[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    requests = types.ModuleType("requests")
    requests.sent = []
    requests.script = []

    def post(url, data=None, headers=None):
        requests.sent.append({"url": url, "body": json.loads(data), "headers": headers})
        return requests.script.pop(0)

    requests.post = post
    monkeypatch.setitem(sys.modules, "requests", requests)
    spec = importlib.util.spec_from_file_location("vtos_agent_test", ROOT / "modules/scripts/bin/agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.WORK = str(tmp_path / "agent")
    module.CONFIG = str(tmp_path / "agent.json")
    module.requests_stub = requests
    return module


CFG = {"api_base": "https://x.test/v1", "api_key": "sk-secret", "model": "m", "assemblyai_key": ""}


def run(agent, user, confirm=lambda p: True, messages=None):
    out = []
    messages = [] if messages is None else messages
    agent.chat_turn(CFG, messages, user, confirm, out.append)
    return "".join(out), messages


def test_streams_plain_text_and_records_history(agent):
    agent.requests_stub.script.append(FakeResponse(text_chunks("Hel", "lo ", "there")))
    out, messages = run(agent, "hi")
    assert out == "Hello there\n"
    assert messages[-1] == {"role": "assistant", "content": "Hello there"}
    sent = agent.requests_stub.sent[0]
    assert sent["url"] == "https://x.test/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-secret"
    assert sent["body"]["stream"] is True and len(sent["body"]["tools"]) == 4


def test_write_file_tool_loop_with_confirmation(agent):
    r = agent.requests_stub
    r.script += [FakeResponse(tool_call("call_1", "write_file", {"name": "todo.txt", "content": "milk\neggs\n"})),
                 FakeResponse(text_chunks("Saved."))]
    asked = []
    out, messages = run(agent, "save a list", confirm=lambda p: asked.append(p) or True)
    assert asked == ["write todo.txt (10 bytes)? y/n "]
    assert (Path(agent.WORK) / "todo.txt").read_text() == "milk\neggs\n"
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "write_file"
    assert messages[2]["tool_call_id"] == "call_1" and "ok: wrote 10 bytes" in messages[2]["content"]
    # the second request carries the tool result back to the model
    assert r.sent[1]["body"]["messages"][-1]["role"] == "tool"
    assert "Saved." in out


def test_declined_write_does_not_touch_disk(agent):
    agent.requests_stub.script += [FakeResponse(tool_call("c", "write_file", {"name": "a.txt", "content": "x"})),
                                   FakeResponse(text_chunks("ok"))]
    out, messages = run(agent, "write", confirm=lambda p: False)
    assert not (Path(agent.WORK) / "a.txt").exists()
    assert messages[2]["content"] == "declined by the user"


def test_list_read_and_grep_tools(agent):
    agent.tool_write("a.txt", "Alpha\nbeta\nGAMMA beta\n", lambda p: True)
    agent.tool_write("b.txt", "nothing here\nBeta again\n", lambda p: True)
    assert agent.tool_list() == "a.txt 22\nb.txt 24"
    assert agent.tool_read("a.txt").startswith("Alpha")
    assert agent.tool_grep("beta", "a.txt") == "2: beta\n3: GAMMA beta"
    both = agent.tool_grep("BETA").splitlines()
    assert both == ["a.txt:2: beta", "a.txt:3: GAMMA beta", "b.txt:2: Beta again"]
    assert agent.tool_grep("zzz") == "no matches"


@pytest.mark.parametrize("bad", ["../x", "a/b", ".hidden", "", "x" * 49, "sp ace", "a..b", "/etc/passwd", None])
def test_sandbox_rejects_bad_names(agent, bad):
    assert not agent.name_ok(bad)
    assert agent.tool_read(bad).startswith("error")
    assert agent.tool_write(bad, "x", lambda p: True).startswith("error")


def test_api_error_is_reported_not_raised(agent):
    agent.requests_stub.script.append(
        FakeResponse([], status=401, text=json.dumps({"error": {"message": "bad key"}})))
    out, messages = run(agent, "hi")
    assert "API error 401: bad key" in out


def test_tool_loop_stops_after_max_steps(agent):
    agent.requests_stub.script += [FakeResponse(tool_call("c%d" % i, "list_files", {}))
                                   for i in range(agent.MAX_STEPS)]
    out, _ = run(agent, "loop")
    assert "stopped after %d steps" % agent.MAX_STEPS in out


def test_history_is_trimmed_and_starts_with_a_user_turn(agent):
    messages = []
    for i in range(40):
        messages.append({"role": "user", "content": "question %d " % i + "x" * 300})
        messages.append({"role": "assistant", "content": "answer " + "y" * 300})
    agent._trim(messages)
    assert len(json.dumps(messages)) <= agent.HISTORY_CHARS
    assert messages[0]["role"] == "user"
    messages = [{"role": "tool", "tool_call_id": "z", "content": "orphan"},
                {"role": "user", "content": "x" * (agent.HISTORY_CHARS + 10)}]
    agent._trim(messages)
    assert messages[0]["role"] == "user"


def test_set_and_config_commands_hide_secrets(agent, capsys):
    agent.main(None, ["set", "api_key", "sk-abcdef123456"])
    agent.main(None, ["set", "model", "gpt-test"])
    agent.main(None, ["config"])
    shown = capsys.readouterr().out
    assert "sk-abcdef123456" not in shown and "sk-... (15 chars)" in shown and "gpt-test" in shown
    assert agent.load_config()["api_key"] == "sk-abcdef123456"
    agent.main(None, ["set", "nope", "1"])
    assert "usage: agent set" in capsys.readouterr().out


def test_no_cpython_only_string_methods_in_device_code():
    text = (ROOT / "modules/scripts/bin/agent.py").read_text()
    for bad in (".ljust(", ".rjust(", ".center(", ".zfill(", ".removeprefix(", "f\""):
        assert bad not in text, bad
