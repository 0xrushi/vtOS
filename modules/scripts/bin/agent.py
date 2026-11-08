#
# MicroPython minimal chat agent (pi-style): one loop, four file tools
# License: MIT
#
#   agent                    chat (type /help)
#   agent <question...>      one question, then exit
#   agent set KEY VALUE      api_base, api_key, model, assemblyai_key
#   agent config             show settings (secrets hidden)
#
# Talks to any OpenAI-compatible /chat/completions endpoint, streamed.
# Tools: list_files, read_file, grep_file, write_file, on one flat folder
# (/flash/agent). Writes ask for confirmation. Settings: /flash/.agent.
#

import gc
import json
import os
import sys

import requests

CONFIG = "/flash/.agent"
WORK = "/flash/agent"
KEYS = ("api_base", "api_key", "model", "assemblyai_key")
DEFAULTS = {"api_base": "https://api.openai.com/v1", "api_key": "", "model": "",
            "assemblyai_key": ""}

MAX_STEPS = 8
HISTORY_CHARS = 12000
READ_MAX = 3000
GREP_MAX = 20
SYSTEM = (
    "You are a helpful assistant on a handheld with a 40x30 character screen. "
    "Reply briefly in plain text, no markdown. Tools work on one flat folder "
    "of text files: list_files, read_file, grep_file, write_file. Look with "
    "the tools before answering about files. Write only what the user asks."
)
TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List files with sizes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file (first 3000 bytes).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "grep_file",
        "description": "Find lines containing text (case-insensitive). Omit name to search all files.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "name": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a text file (user confirms).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "content": {"type": "string"}},
            "required": ["name", "content"]}}},
]

_ALLOWED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


# ---- settings -------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, "r") as f:
            cfg.update(json.loads(f.read()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG, "w") as f:
        f.write(json.dumps(cfg))


def _mask(value):
    if not value:
        return "(empty)"
    return value[:3] + "... (%d chars)" % len(value)


# ---- file tools (flat folder, no paths, no delete) ----------------------------

def name_ok(name):
    if not isinstance(name, str) or not name or len(name) > 48 or name[0] == ".":
        return False
    for ch in name:
        if ch not in _ALLOWED:
            return False
    return ".." not in name


def _path(name):
    try:
        os.mkdir(WORK)
    except OSError:
        pass
    return WORK + "/" + name


def tool_list():
    try:
        names = sorted(os.listdir(WORK))
    except OSError:
        return "(no files)"
    lines = []
    for n in names:
        try:
            lines.append("%s %d" % (n, os.stat(WORK + "/" + n)[6]))
        except OSError:
            pass
    return "\n".join(lines) if lines else "(no files)"


def tool_read(name):
    if not name_ok(name):
        return "error: bad file name"
    try:
        with open(_path(name), "r") as f:
            return f.read(READ_MAX)
    except OSError:
        return "error: cannot read " + name


def _grep_one(name, pattern, out, prefix):
    try:
        f = open(WORK + "/" + name, "r")
    except OSError:
        return
    try:
        n = 0
        for line in f:
            n += 1
            if pattern in line.lower():
                text = line.rstrip("\r\n")[:100]
                out.append(("%s:%d: %s" % (name, n, text)) if prefix else ("%d: %s" % (n, text)))
                if len(out) >= GREP_MAX:
                    return
    finally:
        f.close()


def tool_grep(pattern, name=None):
    if not pattern:
        return "error: missing pattern"
    pattern = pattern.lower()
    out = []
    if name:
        if not name_ok(name):
            return "error: bad file name"
        _grep_one(name, pattern, out, False)
    else:
        try:
            names = sorted(os.listdir(WORK))
        except OSError:
            names = []
        for n in names:
            if name_ok(n):
                _grep_one(n, pattern, out, True)
            if len(out) >= GREP_MAX:
                break
    return "\n".join(out) if out else "no matches"


def tool_write(name, content, confirm):
    if not name_ok(name):
        return "error: bad file name"
    if not isinstance(content, str):
        return "error: missing content"
    if not confirm("write %s (%d bytes)? y/n " % (name, len(content))):
        return "declined by the user"
    try:
        with open(_path(name), "w") as f:
            f.write(content)
    except OSError:
        return "error: write failed"
    return "ok: wrote %d bytes to %s" % (len(content), name)


def run_tool(name, args, confirm, say):
    if name == "list_files":
        say("[list]\n")
        return tool_list()
    if name == "read_file":
        say("[read %s]\n" % args.get("name"))
        return tool_read(args.get("name"))
    if name == "grep_file":
        say("[grep %s]\n" % args.get("pattern"))
        return tool_grep(args.get("pattern"), args.get("name"))
    if name == "write_file":
        result = tool_write(args.get("name"), args.get("content"), confirm)
        say("[%s]\n" % result)
        return result
    return "error: unknown tool " + str(name)


# ---- one streamed completion -----------------------------------------------------

def stream_completion(cfg, messages, say):
    """POST the conversation; print text as it streams. Returns
    (text, calls, error) where calls is a list of {id, name, args}."""
    body = json.dumps({
        "model": cfg["model"], "stream": True,
        "messages": [{"role": "system", "content": SYSTEM}] + messages,
        "tools": TOOLS, "tool_choice": "auto",
    })
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
        "Accept": "text/event-stream",
    }
    url = cfg["api_base"].rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(url, data=body.encode(), headers=headers)
    except (OSError, ValueError) as err:
        return "", [], "connect failed: %s" % err
    body = None
    gc.collect()
    try:
        if resp.status_code != 200:
            try:
                detail = json.loads(resp.text).get("error", {}).get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            return "", [], "API error %s: %s" % (resp.status_code, str(detail)[:120])
        text = ""
        calls = {}
        raw = resp.raw
        while True:
            line = raw.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                text += piece
                say(piece)
            for tc in delta.get("tool_calls") or []:
                call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    call["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    call["name"] = fn["name"]
                if fn.get("arguments"):
                    call["args"] += fn["arguments"]
        return text, [calls[i] for i in sorted(calls)], None
    except OSError as err:
        return "", [], "read failed: %s" % err
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ---- the loop ------------------------------------------------------------------------

def _trim(messages):
    """Keep the history small: drop the oldest turns, never leaving a tool
    result without its call at the front."""
    while len(messages) > 1 and len(json.dumps(messages)) > HISTORY_CHARS:
        messages.pop(0)
        while messages and messages[0].get("role") != "user":
            messages.pop(0)


def chat_turn(cfg, messages, user_text, confirm, say):
    messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_STEPS):
        _trim(messages)
        text, calls, error = stream_completion(cfg, messages, say)
        if error:
            say("\n" + error + "\n")
            return
        if not calls:
            if text:
                messages.append({"role": "assistant", "content": text})
            say("\n")
            return
        messages.append({
            "role": "assistant", "content": text or None,
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                           for c in calls]})
        for c in calls:
            try:
                args = json.loads(c["args"] or "{}")
            except ValueError:
                args = {}
            result = run_tool(c["name"], args, confirm, say)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        gc.collect()
    say("(stopped after %d steps)\n" % MAX_STEPS)


def _say(text):
    sys.stdout.write(text)


def _confirm(prompt):
    answer = input(prompt)
    return answer.strip().lower().startswith("y")


def _ready(cfg):
    if not cfg["api_key"] or not cfg["model"]:
        print("not configured. Set them:")
        print("  agent set api_key YOUR_KEY")
        print("  agent set model MODEL_NAME")
        print("  agent set api_base URL   (default OpenAI)")
        return False
    return True


def main(env, args):
    args = list(args)
    cfg = load_config()
    if args and args[0] == "set":
        if len(args) < 3 or args[1] not in KEYS:
            print("usage: agent set KEY VALUE   keys: " + " ".join(KEYS))
            return
        cfg[args[1]] = " ".join(args[2:])
        save_config(cfg)
        print("saved " + args[1])
        return
    if args and args[0] == "config":
        print("api_base: " + cfg["api_base"])
        print("model: " + (cfg["model"] or "(empty)"))
        print("api_key: " + _mask(cfg["api_key"]))
        print("assemblyai_key: " + _mask(cfg["assemblyai_key"]))
        return
    if not _ready(cfg):
        return

    messages = []
    if args:
        chat_turn(cfg, messages, " ".join(args), _confirm, _say)
        return
    print("agent (%s). /help, /reset, /quit" % cfg["model"])
    while True:
        try:
            line = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/q", "/exit"):
            return
        if line == "/reset":
            messages = []
            print("conversation cleared")
            continue
        if line == "/help":
            print("type to chat. /reset new conversation, /quit leave")
            print("files live in " + WORK)
            continue
        try:
            chat_turn(cfg, messages, line, _confirm, _say)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        gc.collect()
