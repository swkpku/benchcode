#!/usr/bin/env python3
"""benchcode: what a coding agent would feel on this local model stack.

    python3 benchcode.py [http://127.0.0.1:8000] [--model M] [--quick | --deep] [--json] [--out FILE]

Talks only to the server's OpenAI-compatible endpoint (/v1/models, /v1/chat/completions) and to macOS.
Measures writing and reading speed, follow-up TTFT and prompt-cache saving at each context depth the window
and the time budget allow (4K, 16K, 32K, 64K, 128K), writing at depth, the cost of one agent step at depth,
recall of planted facts at depth, the real context window and what happens past it, cache survival,
tool-call fidelity, thinking mode, protocol conformance, sustained speed, memory, and the machine's
conditions. The writing, reading, follow-up, sustain and memory rows follow Kapitsa bench's protocol (same
corpus slices, sampling, thinking switches, nonce, middle-window rate); the reading prompt additionally
carries three planted lines, so it is the same length but not byte-identical. Standard library only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "0.3.0"
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")

# ---------------------------------------------------------------- protocol shared with Kapitsa bench (bench/harness/bench.py)
CORPUS_FILE = os.path.join(HERE, "corpus", "bench_corpus.txt")
CORPUS_SHA256 = "58a7756fd94fc48a7b7306d330b2c44663bdf22faa943f82430966c2878081b2"  # corpus/NOTICE.md
CHARS_PER_TOKEN = 4.25          # corpus characters per token, the harness's heuristic
REF_PROMPT_TOKENS = {4000: 4133, 16000: 16286}  # bench.py's reference counts for its exact prompts (Qwen tokenizers)
WARM_TOKENS = 2700              # warm-up prompt; its slice is unused by prompts up to 16K
WARM_OFFSET_CHARS = 100000
STEP_OFFSET_CHARS = 300000      # agent-step "tool output" slices come from here
COLD_CACHE_MAX = 16             # a cold prompt may share only "<|im_start|>system\n[session " with anything
WINDOW_HEAD, WINDOW_TAIL = 16, 4
DECODE_TOKENS = 512
REP_THRESHOLD = 0.5             # distinct-4-gram ratio below this is a repetition loop (bench.py default)
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
            "frequency_penalty": 0.0, "repeat_penalty": 1.0}
DECODE_USER = ("Write a complete, well-commented Python module that implements an LRU cache with TTL expiry, "
               "a thread-safe wrapper, and a pytest test suite. Write at least 600 lines. Do not stop early.")
DECODE_SYS = "You are a helpful coding assistant. Answer with code only."
SYS = ("You are Nace, a local coding agent. You help the user with their repository. "
       "Be precise. Use tools when you need to read files or run commands.")
FOLLOWUPS = ["Now name one function defined in that file.",
             "Name one class defined in that file, or say there is none.",
             "Which modules does that file import?"]
WARMUP_QUESTION = "Summarize the first file in the repository context."

# ---------------------------------------------------------------- benchcode's additions
SIZES = [4000, 16000, 32000, 64000, 128000]
STEP_TOKENS = 4000              # the tool result appended for the agent-step row
STEP_ANSWER = 64
DEPTH_WRITE_RUNS = 3
DEPTH_WRITE_USER = "New task, unrelated to the repository above. Answer with code only. " + DECODE_USER
LADDER_BUDGET_S = 15 * 60      # wall-clock budget for the rungs past 32K (--deep: none)
WINDOW_FRACTION = 0.75          # the largest rung is at most this share of the declared window
CACHE_SURVIVAL_OFFSET = 400000
WINDOW_PROBE_OFFSET = 200000
RECALL_NEEDLES = [              # (line planted in the document, question, acceptable answer fragments)
    ("# NOTE: the on-call rota for this service is pinned to the fridge door in the Lisbon office kitchen.",
     "Where would someone find the schedule of who is on call for this service?", ["fridge", "lisbon", "kitchen"]),
    ("# Reminder from Priya: her cat is called Bramble and only eats at dusk.",
     "What is the name of Priya's pet?", ["bramble"]),
    ("# The staging database credential is rotated every second Thursday by the platform team.",
     "How often does the platform team change the staging database password?",
     ["second thursday", "every other thursday", "alternate thursday", "two weeks", "fortnight", "biweekly", "bi weekly", "14 days", "every 2 weeks"]),
]
RECALL_QUESTION = ("Using only the repository context above, answer each question in one short line, numbered 1 to 3:\n"
                   + "\n".join(f"{i + 1}. {q}" for i, (_, q, _) in enumerate(RECALL_NEEDLES)))
LEAK_RE = re.compile(r"<tool_call>|<function=|<\|channel\|>|<tool_response>|\[TOOL_CALL\]", re.I)

TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command and return its output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_ms": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read", "description": "Read a file. Returns the content with line numbers.",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "offset": {"type": "integer", "description": "First line (1-based)"},
            "limit": {"type": "integer", "description": "Number of lines"}}, "required": ["filePath"]}}},
    {"type": "function", "function": {"name": "write", "description": "Create or overwrite a file with the given content.",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filePath", "content"]}}},
    {"type": "function", "function": {"name": "edit", "description": "Replace oldString with newString in a file. oldString must match exactly once.",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "oldString": {"type": "string"}, "newString": {"type": "string"},
            "replaceAll": {"type": "boolean"}}, "required": ["filePath", "oldString", "newString"]}}},
    {"type": "function", "function": {"name": "glob", "description": "Find files by glob pattern.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "grep", "description": "Search file contents with a regular expression.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}}},
]
TOOL_SPECS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
TOOL_SYSTEM = ("You are a coding agent working in a repository. Use the provided tools to act. "
               "Call exactly one tool for the user's request. Do not explain, do not ask questions.")
TOOL_TRIALS = [  # (prompt, acceptable tools)
    ("Read the file src/main.py.", {"read"}),
    ("Create a file named notes/todo.md whose content is exactly: buy milk", {"write"}),
    ("Run the test suite with: python3 -m pytest -q", {"bash"}),
    ("In config.py replace the line `TIMEOUT = 30` with `TIMEOUT = 60`.", {"edit"}),
    ("Find every Python file under the src directory.", {"glob", "bash", "grep"}),
    ("Search the repository for the string TODO.", {"grep", "bash"}),
    ("Write a C++ hello world program to hello.cpp. It must #include <iostream> and print \"Hello, world!\" with a newline.", {"write"}),
    ("Read lines 10 to 20 of README.md.", {"read"}),
    ("Run `ls -la` in the current directory with a timeout of 5000 milliseconds.", {"bash"}),
    ("Create data/sample.json containing a JSON object with keys \"name\": \"bench\" and \"count\": 3.", {"write"}),
    ("Replace every occurrence of `foo_bar` with `fooBar` in utils.py.", {"edit", "bash"}),
    ("Show the first 5 lines of the file logs/app.log.", {"read", "bash"}),
    ("List all markdown files in the docs folder.", {"glob", "bash"}),
    ("Which files mention the function parse_config? Search for it.", {"grep", "bash"}),
    ("Append the line `DEBUG = True` to settings.py by editing the file (it currently ends with `PORT = 8080`).", {"edit", "write", "bash"}),
    ("Read pyproject.toml.", {"read"}),
    ("Run git status.", {"bash"}),
    ("Create an empty file called .keep inside the build directory.", {"write", "bash"}),
    ("Find files matching *.test.ts anywhere in the project.", {"glob", "bash", "grep"}),
    ("Open the file at path src/handlers/auth.py and show me its content.", {"read"}),
]
DEFAULT_THINK_TOKENS = 4096     # reply cap when no thinking switch is sent (reasoning + the tool call)
SESSION_STEPS = 8               # agent session: tool results appended one per step
SESSION_RESULT_TOKENS = 8000    # each tool result (a page snapshot, a long file, a test log)
SESSION_PROMPT_TOKENS = 12000   # the harness's system prompt: instructions, tool list, project notes
SESSION_RESULT_OFFSET = 100000
SESSION_BUDGET_S = 10 * 60      # default wall-clock budget; fewer steps when the reading rate is low (--deep: all steps)
SESSION_SYSTEM = ("You are a coding agent working in a repository. Use the provided tools to act. "
                  "Call one tool at a time. Do not ask questions.")
SESSION_TASK = ("Find where attention masks are built in this repository and work out which files would have to change "
                "to support a new mask type. Use the tools and read what you need before answering.")
SESSION_CALLS = [("grep", {"pattern": "attention_mask", "path": "src"}), ("read", {"filePath": "src/models/modeling_a.py"}),
                 ("read", {"filePath": "src/models/modeling_b.py"}), ("bash", {"command": "python3 -m pytest tests/test_masks.py -q"}),
                 ("read", {"filePath": "src/models/modeling_c.py"}), ("grep", {"pattern": "def _prepare_.*mask", "path": "src"}),
                 ("read", {"filePath": "src/models/modeling_d.py"}), ("bash", {"command": "git log --stat -5"})]
COMPACT_PROMPT = ("The conversation is about to exceed the context window. Write a detailed summary of it so the work can continue "
                  "in a new conversation. Cover: 1. the user's request and intent, 2. the files and code sections examined and what "
                  "each showed, 3. commands run and their results, 4. conclusions so far, 5. what remains to be done and the next step. "
                  "Write the summary as plain text now. Do not call any tool.")
REJECT_STATUSES = (400, 413, 422)   # a prompt refused for its size; other 4xx are errors, not window facts


# ---------------------------------------------------------------- terminal
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Term:
    """Colours and an in-place live line, only when stdout is a terminal (Kapitsa bench's conventions)."""

    def __init__(self, out):
        self.out = out
        self.tty = hasattr(out, "isatty") and out.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.live_text = None
        self._stop = threading.Event()
        self._thread = None
        self._label = ""
        self._detail = None
        self._t0 = 0.0

    def paint(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.tty else text

    def mark(self, m):
        return {"✓": self.paint(32, m), "!": self.paint(33, m), "✗": self.paint(31, m), "·": self.paint(2, m)}.get(m, m)

    def dim(self, text):
        return self.paint(2, text)

    def start(self, label, detail=None):
        """Show `label` with a spinner and elapsed seconds; `detail` is a callable returning the live note."""
        self.stop()
        if not self.tty:
            return
        self._label, self._detail, self._t0 = label, detail, time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            el = time.monotonic() - self._t0
            note = ""
            try:
                note = self._detail() if self._detail else ""
            except Exception:
                pass
            elapsed = f"{el:.0f} s" if el >= 1 else ""
            line = f"  {self.paint(36, SPINNER[i % len(SPINNER)])} {self._label:<20} {elapsed:<16} {self.dim(note or '')}"
            self.out.write("\r\x1b[2K" + line)
            self.out.flush()
            i += 1
            self._stop.wait(0.1)

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=1)
            self._thread = None
            self.out.write("\r\x1b[2K")
            self.out.flush()


# ---------------------------------------------------------------- small helpers
def sh(*argv, timeout=15):
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout.strip()
    except Exception:
        return ""


def sysctl(name):
    return sh("sysctl", "-n", name) or None


def med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def distinct4(text):
    w = re.findall(r"\w+|[^\w\s]", text or "")
    if len(w) < 8:
        return None
    g = [tuple(w[i:i + 4]) for i in range(len(w) - 3)]
    return round(len(set(g)) / len(g), 4)


def normalise(s):
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip() + " "


def fmt_tps(v):
    return "–" if v is None else f"{v:.1f} tok/s" if v < 100 else f"{v:.0f} tok/s"


def fmt_s(v):
    return "–" if v is None else f"{v:.2f} s" if v < 10 else f"{v:.1f} s" if v < 100 else f"{v:.0f} s"


def fmt_gb(b):
    return "–" if not b else f"{b / 2**20:.0f} MB" if b < 2**30 else f"{b / 2**30:.1f} GB"


def fmt_k(tokens):
    if not tokens:
        return "–"
    k = tokens / 1000
    return f"{k:.0f}K" if k >= 10 or abs(k - round(k)) < 0.05 else f"{k:.1f}K"


def load_corpus():
    if not os.path.exists(CORPUS_FILE):
        raise SystemExit(f"no prompt corpus at {CORPUS_FILE}")
    with open(CORPUS_FILE, encoding="utf-8", newline="") as fh:
        text = fh.read()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def reasoning_count(r):
    """Reasoning tokens in a reply: the usage count when the server gives one, else a share of completion tokens by characters."""
    if r.reasoning_tokens is not None:
        return r.reasoning_tokens
    if r.reasoning and r.completion_tokens:
        return int(round(r.completion_tokens * len(r.reasoning) / max(1, len(r.reasoning) + len(r.content))))
    return 0 if r.completion_tokens else None


def tool_call_problem(r):
    """None when the reply is one well-formed structured call to a known tool; otherwise what is wrong with it."""
    if not r.tool_calls:
        if r.finish == "length":
            return "cut off by max_tokens before any call"
        return "tool markup in text, not a structured call" if LEAK_RE.search(r.content) else "no structured tool call"
    tc = r.tool_calls[0]
    if tc["name"] not in TOOL_SPECS:
        return f"unknown tool {tc['name']!r}"
    try:
        args = json.loads(tc["arguments"] or "{}")
    except Exception:
        args = None
    if not isinstance(args, dict):
        return "arguments not a JSON object"
    spec = TOOL_SPECS[tc["name"]]
    missing = [k for k in spec.get("required", []) if k not in args]
    if missing:
        return f"missing {missing}"
    bad = []
    for k, v in args.items():
        want = (spec["properties"].get(k) or {}).get("type")
        if (want == "integer" and not (isinstance(v, int) and not isinstance(v, bool))) or (want == "string" and not isinstance(v, str)):
            bad.append(k)
    if bad:
        return f"wrong types {bad}"
    if LEAK_RE.search(r.content):
        return "leaked markup beside a structured call"
    return None


# ---------------------------------------------------------------- one streamed chat completion
class Chat:
    def __init__(self):
        self.ok = False
        self.status = None
        self.error = None
        self.ttft = None
        self.total = None
        self.usage = {}
        self.timings = {}
        self.content = ""
        self.reasoning = ""
        self.tool_calls = []
        self.finish = None
        self.ev_t, self.ev_c = [], []

    @property
    def prompt_tokens(self):
        return self.usage.get("prompt_tokens")

    @property
    def completion_tokens(self):
        return self.usage.get("completion_tokens")

    @property
    def reasoning_tokens(self):
        return (self.usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    @property
    def cached_tokens(self):
        """Cached prompt tokens as the endpoint reports them: usage.prompt_tokens_details.cached_tokens, or a
        timings.cache_n field in the final event (bench.py cached_of takes the larger); None when neither exists."""
        vals = []
        c = (self.usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if isinstance(c, (int, float)):
            vals.append(int(c))
        c = (self.timings or {}).get("cache_n")
        if isinstance(c, (int, float)):
            vals.append(int(c))
        return max(vals) if vals else None

    def draft_acceptance(self):
        """Accepted drafts over drafts, if the final event carries timings.draft_n (as bench.py t_decode records)."""
        tm = self.timings or {}
        if tm.get("draft_n"):
            return tm.get("draft_n_accepted", 0) / tm["draft_n"]
        return None

    def middle_rate(self, head=WINDOW_HEAD, tail=WINDOW_TAIL):
        """bench.py decode_rates: tokens per second between the event completing token `head` and the last
        event at or before token ct-tail; tokens are assigned to events in proportion to their characters."""
        ct = self.completion_tokens
        t, c = self.ev_t, self.ev_c
        if len(t) < 2 or not ct:
            return None
        if len(t) == ct:
            cum = list(range(1, ct + 1))
        else:
            tot = sum(c)
            if tot <= 0:
                return None
            acc, cum = 0, []
            for x in c:
                acc += x
                cum.append(acc * ct / tot)
        i = next((k for k in range(len(t)) if cum[k] >= head), None)
        js = [k for k in range(len(t)) if cum[k] <= ct - tail]
        j = js[-1] if js else None
        if i is None or j is None or j <= i or t[j] <= t[i]:
            return None
        return (cum[j] - cum[i]) / (t[j] - t[i])

    def rate_problem(self):
        if not self.completion_tokens:
            return "usage.completion_tokens missing"
        if len(self.ev_t) < 2:
            return "too few streamed events for a middle-window rate"
        return None

    def thinking_leak(self):
        out = []
        if self.reasoning.strip():
            out.append(f"reasoning text returned ({len(self.reasoning)} chars)")
        c = self.content.lstrip()
        if c.startswith("<think>"):
            out.append("content starts with <think>")
        elif "</think>" in self.content:
            out.append("content contains </think>")
        return out

    def brief(self):
        return {"status": self.status, "error": self.error, "ttft_s": None if self.ttft is None else round(self.ttft, 4),
                "total_s": None if self.total is None else round(self.total, 3), "prompt_tokens": self.prompt_tokens,
                "cached_tokens": self.cached_tokens, "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens, "finish": self.finish, "tool_calls": [tc["name"] for tc in self.tool_calls],
                "content_chars": len(self.content), "reasoning_chars": len(self.reasoning), "events": len(self.ev_t),
                "timings": self.timings or None}


class Server:
    """The OpenAI-compatible endpoint. Nothing here depends on which engine is behind it."""

    def __init__(self, base_url, model=None):
        base = base_url.rstrip("/")
        self.base = base[:-3] if base.endswith("/v1") else base
        self.model = model
        self.process = None          # name of the process listening on the port, for the header only
        self.pid = None
        self.sampling = dict(SAMPLING)
        self.dropped_fields = []
        self.no_think = {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
        self.no_think_style = "both"
        self.thinks_by_default = None   # None = untested
        self.think_switchable = None
        self.entry = None
        self.model_ids = []
        self.extensions_used = []
        self.progress = {"t0": None, "first": None, "tokens": 0, "chars": 0}

    def progress_note(self):
        """What the current request is doing, for the live line."""
        p = self.progress
        if p["t0"] is None:
            return ""
        if p["first"] is None:
            return "waiting for the first token"
        el = time.perf_counter() - p["first"]
        toks = p["chars"] / 3.8
        return f"{toks:,.0f} tokens so far" + (f", {toks / el:.1f} tok/s" if el > 0.5 and toks > 8 else "")

    @property
    def always_reasons(self):
        """bench.py's --always-reasons case: the model reasons and no switch stops it."""
        return bool(self.thinks_by_default) and self.think_switchable is False

    def _req(self, path, body=None, timeout=30, session=None):
        hdrs = {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("BENCH_API_KEY", "local")}
        if session:  # bench.py post(): a stable per-conversation id for engines that key their cache on it
            hdrs["X-Session-Id"] = session
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=hdrs), timeout=timeout)

    def get_json(self, path, timeout=10):
        try:
            with self._req(path, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return None, None

    def detect(self, port):
        out = sh("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fcp")
        for line in out.splitlines():
            if line.startswith("p") and self.pid is None:
                self.pid = int(line[1:])
            elif line.startswith("c") and self.process is None:
                self.process = line[1:]
        st, m = self.get_json("/v1/models", 10)
        data = (m or {}).get("data", []) if isinstance(m, dict) else []
        self.model_ids = [e.get("id") for e in data][:40]
        if not self.model:
            active = [e for e in data if e.get("active")]
            if active:
                self.extensions_used.append("active")
            self.model = (active or data or [{}])[0].get("id")
        self.entry = next((e for e in data if e.get("id") == self.model), None)
        return st, len(data)

    def pids(self):
        if not self.pid:
            return []
        seen, todo = [], [self.pid]
        while todo:
            p = todo.pop()
            if p in seen:
                continue
            seen.append(p)
            todo += [int(x) for x in sh("pgrep", "-P", str(p)).split()]
        return seen

    def declared_window(self):
        """The context window if the /v1/models entry declares one (a common extension; not in the OpenAI schema)."""
        e = self.entry or {}
        for key in ("context_fit", "context_length", "max_model_len", "context_window", "max_context_length"):
            v = e.get(key)
            if isinstance(v, (int, float)) and v > 0:
                self.extensions_used.append(key)
                return int(v), key
        return None, None

    def body(self, msgs, max_tokens, greedy=False, think=False, tools=None, tool_choice=None):
        s = dict(self.sampling)
        if greedy:
            s["temperature"], s["top_p"] = 0.0, 1.0
            if "top_k" in s:
                s["top_k"] = 1
        b = {"model": self.model, "messages": msgs, "max_tokens": max_tokens, **s}
        if think == "default":  # no switch at all: whatever the server and template do when a harness sends nothing
            pass
        elif think:  # bench.py think_on: reasoning_effort low, enable_thinking true
            b["reasoning_effort"] = "low"
            if self.no_think_style in ("both", "kwargs"):
                b["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            b.update(self.no_think)
        if tools:
            b["tools"] = tools
        if tool_choice is not None:
            b["tool_choice"] = tool_choice
        return b

    def chat(self, body, timeout=1800, wall_cap=None, session=None):
        body = dict(body)
        body["stream"] = True
        body.setdefault("stream_options", {"include_usage": True})
        r = Chat()
        t0 = time.perf_counter()
        first = None
        partial = {}
        self.progress = {"t0": t0, "first": None, "tokens": 0, "chars": 0}
        try:
            with self._req("/v1/chat/completions", body, timeout=timeout, session=session) as resp:
                r.status = resp.status
                for raw in resp:
                    now = time.perf_counter()
                    if wall_cap and now - t0 > wall_cap:
                        r.error = f"stopped after {wall_cap:.0f} s"
                        break
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    if ev.get("error"):  # an in-band error after HTTP 200 (overflow, OOM once headers were sent)
                        r.error = "stream error: " + json.dumps(ev["error"])[:700]
                        break
                    if ev.get("usage"):
                        r.usage = ev["usage"]
                    if ev.get("timings"):
                        r.timings = ev["timings"]
                    for ch in ev.get("choices") or []:
                        d = ch.get("delta") or {}
                        if ch.get("finish_reason"):
                            r.finish = ch["finish_reason"]
                        got, nch = False, 0
                        if d.get("content"):
                            r.content += d["content"]
                            nch += len(d["content"])
                            got = True
                        rc = d.get("reasoning_content") or d.get("reasoning")
                        if rc:
                            r.reasoning += rc
                            nch += len(rc)
                            got = True
                        for tc in d.get("tool_calls") or []:
                            got = True
                            e = partial.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                            if tc.get("id"):
                                e["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                e["name"] += fn["name"]
                                nch += len(fn["name"])
                            if fn.get("arguments"):
                                s_ = fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(fn["arguments"])
                                e["arguments"] += s_
                                nch += len(s_)
                        if got:
                            if first is None:
                                first = now
                                self.progress["first"] = now
                            r.ev_t.append(now - t0)
                            r.ev_c.append(nch)
                            self.progress["chars"] += nch
            if r.status == 200 and r.error is None and not r.ev_t and not r.usage:
                r.error = "empty stream: no events and no usage"
            r.ok = r.status == 200 and r.error is None
        except urllib.error.HTTPError as e:
            r.status = e.code
            try:
                r.error = f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}"
            except Exception:
                r.error = f"HTTP {e.code}"
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"[:300]
        r.total = time.perf_counter() - t0
        if first is not None:
            r.ttft = first - t0
        r.tool_calls = [partial[k] for k in sorted(partial)]
        return r

    def chat_adapting(self, body, **kw):
        """First contact: drop only a sampling field the server names in a 400; switch the thinking field it rejects."""
        r = self.chat(body, **kw)
        tries = 0
        while r.status == 400 and tries < 6:
            msg = (r.error or "").lower()
            named = [f for f in ("repeat_penalty", "min_p", "top_k", "presence_penalty", "frequency_penalty") if f in self.sampling and f in msg]
            if named:
                self.sampling.pop(named[0])
                self.dropped_fields.append(named[0])
            elif "chat_template_kwargs" in msg and self.no_think_style in ("both", "kwargs"):
                self.no_think_style, self.no_think = "effort", {"reasoning_effort": "none"}
            elif "reasoning_effort" in msg and self.no_think_style in ("both", "effort"):
                self.no_think_style, self.no_think = "kwargs", {"chat_template_kwargs": {"enable_thinking": False}}
            else:
                break
            body = self.body(body["messages"], body["max_tokens"], tools=body.get("tools"))
            r = self.chat(body, **kw)
            tries += 1
        return r


# ---------------------------------------------------------------- macOS
def machine():
    m = {"hw_model": sysctl("hw.model"), "chip": sysctl("machdep.cpu.brand_string"), "arch": platform.machine(),
         "macos": sh("sw_vers", "-productVersion") or platform.mac_ver()[0], "build": sh("sw_vers", "-buildVersion"),
         "ncpu": int(sysctl("hw.ncpu") or 0) or None}
    mem = sysctl("hw.memsize")
    m["memsize_bytes"] = int(mem) if mem and mem.isdigit() else None
    wl = sysctl("iogpu.wired_limit_mb")
    m["gpu_wired_limit_bytes"] = int(wl) * 2**20 if wl and wl.isdigit() else None
    try:
        m["gpu_cores"] = int(json.loads(sh("system_profiler", "SPDisplaysDataType", "-json", timeout=30))["SPDisplaysDataType"][0]["sppci_cores"])
    except Exception:
        m["gpu_cores"] = None
    m["chip_short"] = (m["chip"] or "").replace("Apple ", "").strip()
    try:
        hw = json.loads(sh("system_profiler", "SPHardwareDataType", "-json", timeout=30))["SPHardwareDataType"][0]
        m["name"] = hw.get("machine_name") or m["hw_model"]
    except Exception:
        m["name"] = m["hw_model"]
    m["python"] = sys.version.split()[0]
    return m


def conditions(pids=()):
    c = {}
    batt = sh("pmset", "-g", "batt")
    c["power"] = "AC" if "AC Power" in batt else "battery" if "Battery Power" in batt else "desktop" if "InternalBattery" not in batt else "unknown"
    mm = re.search(r"(\d+)%", batt)
    c["battery_pct"] = int(mm.group(1)) if mm else None
    pm = sh("pmset", "-g")
    mode = None
    for key in ("powermode", "lowpowermode"):
        mm = re.search(rf"^\s*{key}\s+(\d)", pm, re.M)
        if mm:
            mode = int(mm.group(1))
            break
    c["power_mode"] = {0: "automatic", 1: "low power", 2: "high power"}.get(mode, "unknown")
    out = sh("osascript", "-l", "JavaScript", "-e",
             "ObjC.import('Foundation'); $.NSProcessInfo.processInfo.isLowPowerModeEnabled + ' ' + $.NSProcessInfo.processInfo.thermalState")
    parts = out.split()
    c["low_power_mode"] = parts[0] == "true" if parts else None
    try:
        c["thermal"] = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}[int(parts[1])]
    except Exception:
        c["thermal"] = "unknown"
    sw = sysctl("vm.swapusage") or ""
    mm = re.search(r"used = ([\d.]+)([MG])", sw)
    c["swap_used_bytes"] = int(float(mm.group(1)) * (2**30 if mm.group(2) == "G" else 2**20)) if mm else None
    c["loadavg"] = sysctl("vm.loadavg")
    rows = []
    for line in sh("ps", "-Aceo", "pid,pcpu,comm", "-r").splitlines()[1:]:
        p = line.split(None, 2)
        if len(p) == 3:
            try:
                pid, cpu = int(p[0]), float(p[1])
            except ValueError:
                continue
            if pid not in pids and pid != os.getpid() and cpu >= 20:
                rows.append(f"{p[2].split('/')[-1]} {cpu:.0f}%")
    c["busy_processes"] = rows[:4]
    return c


def footprint(pids):
    """phys_footprint, phys_footprint_peak and resident mapped-file bytes of the pids (bench.py footprint_snapshot:
    mmap'd weights are clean file-backed pages that phys_footprint does not count)."""
    now = peak = mapped = 0
    for p in pids:
        out = sh("footprint", "-f", "bytes", "-w", "-p", str(p), timeout=60)
        m1 = re.search(r"phys_footprint:\s*(\d+)\s*B", out)
        if not m1:
            continue
        now += int(m1.group(1))
        m2 = re.search(r"phys_footprint_peak:\s*(\d+)\s*B", out)
        peak += int(m2.group(1)) if m2 else int(m1.group(1))
        mf = re.search(r"^\s*(\d+) B\s+(\d+) B\s+(\d+) B\s+(\d+) B\s+(\d+) B\s+\d+\s+mapped file\s*$", out, re.M)
        mapped += int(mf.group(3)) if mf else 0
    return (now or None), (peak or None), mapped


# ---------------------------------------------------------------- the benchmark
class Bench:
    def __init__(self, args):
        self.a = args
        self.s = Server(args.server, args.model)
        self.corpus, self.corpus_sha = load_corpus()
        self.res = {"benchcode": VERSION, "date": dt.datetime.now().isoformat(timespec="seconds"), "server_url": self.s.base,
                    "corpus_sha256": self.corpus_sha, "corpus_frozen": self.corpus_sha == CORPUS_SHA256,
                    "rows": [], "flags": [], "invalid": [], "depth": {}, "once": {}}
        self.t0 = time.monotonic()
        self.peak_mem = None
        self.mapped_mem = 0
        self.out = sys.stderr if args.json else sys.stdout
        self.term = Term(self.out)
        self.server_down = False
        self.token_ratio = None      # observed prompt_tokens per target token on the first rung

    # ---- output
    def say(self, mark, label, value, note=""):
        self.term.stop()
        self.res["rows"].append({"mark": mark, "label": label, "value": value, "note": note})
        print(f"  {self.term.mark(mark)} {label:<20} {value:<16} {self.term.dim(note) if note else ''}".rstrip(), file=self.out, flush=True)

    def step(self, label, detail=None):
        """Live line while a step runs; the step's say() replaces it."""
        self.term.start(label, detail or self.s.progress_note)

    def separator(self, text):
        self.term.stop()
        print(self.term.dim(f"  ── {text} " + "─" * max(0, 60 - len(text))), file=self.out, flush=True)

    def flag(self, text):
        if text not in self.res["flags"]:
            self.res["flags"].append(text)

    def invalid(self, text):
        if text not in self.res["invalid"]:
            self.res["invalid"].append(text)

    # ---- prompts
    def context(self, target_tokens, needles=False, offset=0):
        need = int(target_tokens * CHARS_PER_TOKEN)
        if offset + need > len(self.corpus):
            raise ValueError(f"a {target_tokens:,}-token prompt at offset {offset:,} needs {need:,} characters; the corpus has {len(self.corpus):,}")
        if not needles:
            return self.corpus[offset:offset + need]
        planted = [n for n, _, _ in RECALL_NEEDLES]
        lines = self.corpus[offset:offset + need - sum(len(x) + 1 for x in planted)].split("\n")
        n = len(lines)
        for frac, needle in zip((0.10, 0.50, 0.90), planted):
            lines.insert(int(n * frac), needle)
        return "\n".join(lines)

    def check_sizes(self, sizes):
        """Every slice this run will cut must fit the corpus, checked before any request (bench.py check_sizes)."""
        asks = [("first request", 1000, WARM_OFFSET_CHARS + 50000), ("warm-up", WARM_TOKENS, WARM_OFFSET_CHARS),
                ("cache survival", 4000, CACHE_SURVIVAL_OFFSET)]
        asks += [(f"reading {fmt_k(s)}", s, 0) for s in sizes]
        asks += [(f"agent step at {fmt_k(s)}", STEP_TOKENS, STEP_OFFSET_CHARS + SIZES.index(s) * 20000) for s in sizes]
        if not self.a.quick:
            asks += [("agent session prompt", SESSION_PROMPT_TOKENS, 0)]
            asks += [(f"agent session result {k + 1}", SESSION_RESULT_TOKENS, SESSION_RESULT_OFFSET + k * int(SESSION_RESULT_TOKENS * CHARS_PER_TOKEN)) for k in range(SESSION_STEPS)]
        for what, tokens, offset in asks:
            need = offset + int(tokens * CHARS_PER_TOKEN)
            if need > len(self.corpus):
                raise SystemExit(f"{what}: needs {need:,} characters of the prompt corpus, which has {len(self.corpus):,}")

    @staticmethod
    def prefill_messages(ctx, nonce, question):
        # the nonce sits right after "<|im_start|>system\n": no engine can reuse more than ~5 tokens
        return [{"role": "system", "content": f"[{nonce}] {SYS}\n\n# Repository context\n{ctx}"},
                {"role": "user", "content": question}]

    @staticmethod
    def nonce(prefix="session"):
        return f"{prefix} {random.getrandbits(64):016x}"

    @staticmethod
    def session_id():
        return f"bench-{random.getrandbits(48):012x}"

    @staticmethod
    def recall_score(text):
        """Each planted fact scored against its own numbered answer line when the answer is numbered, else the whole text."""
        lines = {}
        for ln in (text or "").splitlines():
            m = re.match(r"\s*\**\s*(\d)[.):]\s*(.*)", ln)
            if m and m.group(1) in "123":
                lines.setdefault(m.group(1), m.group(2))
        score = 0
        for i, (_, _, frags) in enumerate(RECALL_NEEDLES, 1):
            hay = normalise(lines.get(str(i), text if len(lines) < 2 else ""))
            if any(normalise(f).strip() and normalise(f) in hay for f in frags):
                score += 1
        return score

    def busy_now(self):
        """Other processes at 50% CPU or more right now (one ps call)."""
        mine = set(self.s.pids()) | {os.getpid()}
        rows = []
        for line in sh("ps", "-Aceo", "pid,pcpu,comm", "-r").splitlines()[1:8]:
            p = line.split(None, 2)
            try:
                if len(p) == 3 and int(p[0]) not in mine and float(p[1]) >= 50:
                    rows.append(f"{p[2].split('/')[-1]} {float(p[1]):.0f}%")
            except ValueError:
                pass
        return rows[:3]

    def mem_sample(self):
        now, peak, mapped = footprint(self.s.pids())
        for v in (now, peak):
            if v and (self.peak_mem is None or v > self.peak_mem):
                self.peak_mem = v
        self.mapped_mem = max(self.mapped_mem, mapped or 0)
        return now

    def answer_tokens(self, n):
        return max(n, 1024) if self.s.always_reasons else n

    def check_cold(self, r, label):
        c = r.cached_tokens
        if c is not None and c > COLD_CACHE_MAX:
            self.invalid(f"{label}: a cold prompt reported {c} cached tokens (the nonce was not honoured)")

    def leak_check(self, r, label):
        """bench.py: any non-thinking request that returns reasoning is INVALID, unless the model always reasons."""
        leak = r.thinking_leak()
        if leak and not self.s.always_reasons:
            self.invalid(f"{label}: " + "; ".join(leak))
            self.res.setdefault("leak_evidence", []).append({"label": label, "content_head": r.content[:400], "content_tail": r.content[-200:],
                                                             "reasoning_head": r.reasoning[:200], "completion_tokens": r.completion_tokens})

    # ---- phases
    def run(self):
        from urllib.parse import urlparse
        st, n_models = self.s.detect(urlparse(self.s.base).port or 80)
        self.res["machine"] = machine()
        self.res["server"] = {"process": self.s.process, "pid": self.s.pid}
        self.res["model"] = self.s.model
        self.res["model_entry"] = self.s.entry
        self.header()
        if st != 200:
            self.say("✗", "Server", "unreachable", f"GET /v1/models returned {st}")
            self.invalid("server did not answer /v1/models")
            return self.finish()
        if not n_models:
            self.say("✗", "Server", "no models", "/v1/models lists nothing")
            self.invalid("no model loaded")
            return self.finish()
        if self.s.entry is None:
            if self.a.model:
                self.flag(f"--model {self.s.model} is not listed by /v1/models (listed: {', '.join(str(x) for x in self.s.model_ids[:6])}); proceeding anyway")
                self.s.entry = {"id": self.s.model}
            else:
                self.invalid("no usable model entry")
                return self.finish()
        if not self.res["corpus_frozen"]:
            self.flag("prompt corpus is not the frozen one; reading rows are not comparable with Kapitsa bench")
        declared, source = self.s.declared_window()
        self.res["window_declared"], self.res["window_source"] = declared, source
        sizes = self.rung_sizes(declared)
        self.check_sizes(sizes)
        self.res["conditions_start"] = conditions(self.s.pids())
        self.res["memory_idle_bytes"] = self.mem_sample()
        if self.res["conditions_start"].get("low_power_mode"):
            self.say("✗", "Conditions", "Low Power Mode", "every speed number would be low; turn it off and re-run")
            self.invalid("Low Power Mode is on")
            return self.finish()
        self.p_quiet()
        try:
            if not self.p_first_request():
                return self.finish()
            self.p_thinking()
            self.p_warmup()
            self.p_writing()
            self.p_ladder(sizes)
            if not self.server_down:
                self.p_window(declared, sizes)
            if not self.server_down:
                self.p_cache_survival()
                self.p_tools()
                self.p_protocol()
                if not self.a.quick:
                    self.p_session(declared)
                if not self.a.quick and not self.server_down:
                    self.p_default_thinking(declared)
                if not self.a.quick and not self.server_down:
                    self.p_sustain(300 if self.a.deep else 180)
            self.res["conditions_end"] = conditions(self.s.pids())
            if self.res["conditions_end"].get("low_power_mode"):
                self.invalid("Low Power Mode was on at the end of the run")
            self.p_memory()
        except KeyboardInterrupt:  # keep what was measured: the partial result is saved and marked
            self.term.stop()
            self.say("✗", "benchcode", "interrupted", "stopped before the end; the rows above are saved")
            self.invalid("interrupted before the end of the run")
        except Exception as e:  # never a traceback and no JSON: record it and finish
            self.say("✗", "benchcode", "internal error", f"{type(e).__name__}: {e}"[:120])
            self.invalid(f"benchcode internal error: {type(e).__name__}: {e}"[:300])
        self.res["request"] = {"sampling": self.s.sampling, "greedy": {"temperature": 0.0, "top_p": 1.0, "top_k": 1},
                               "no_think": self.s.no_think, "stream_options": {"include_usage": True},
                               "dropped_fields": self.s.dropped_fields, "models_extensions_used": sorted(set(self.s.extensions_used))}
        self.res["elapsed_s"] = round(time.monotonic() - self.t0, 1)
        return self.finish()

    def header(self):
        m = self.res["machine"]
        e = self.s.entry or {}
        quant = e.get("quant") or e.get("quantization") or ""
        quant = quant if len(quant) <= 60 else quant[:59] + "…"
        print(f"\n   benchcode {VERSION}  ·  {self.s.base}" + (f"  ·  served by {self.s.process}" if self.s.process else ""), file=self.out)
        print(f"  {m.get('name') or m.get('hw_model')}  ·  {m.get('chip_short')}  ·  {m.get('gpu_cores') or '?'}-core GPU  ·  {fmt_gb(m.get('memsize_bytes'))}  ·  macOS {m.get('macos')}", file=self.out)
        print(f"  {self.s.model}{'  ·  ' + quant + ' (as /v1/models declares it)' if quant else ''}\n", file=self.out)

    def p_quiet(self):
        """Five seconds of CPU sampling before anything is timed (Kapitsa bench waits for a quiet GPU; powermetrics needs root)."""
        self.step("Machine quiet", lambda: "sampling other processes for 5 s")
        seen = {}
        for _ in range(5):
            for row in conditions(self.s.pids()).get("busy_processes") or []:
                name, pct = row.rsplit(" ", 1)
                seen[name] = max(seen.get(name, 0), int(pct.rstrip("%")))
            time.sleep(1.0)
        self.res["once"]["quiet"] = seen
        if seen:
            busy = ", ".join(f"{k} {v}%" for k, v in sorted(seen.items(), key=lambda x: -x[1])[:3])
            self.say("!", "Machine busy", f"{len(seen)} process" + ("es" if len(seen) > 1 else ""), f"over 20% CPU: {busy}; measuring anyway, numbers may be low")
            self.flag(f"contention: {busy} ran during the benchmark")
        else:
            self.say("✓", "Machine quiet", "5 s", "no other process over 20% CPU")

    def p_first_request(self):
        """First request vs the next ones (lazy kernel and cache build), ~1K-token prompts."""
        ctx = self.context(1000, offset=WARM_OFFSET_CHARS + 50000)
        times = []
        self.step("First request", lambda: f"request {len(times) + 1} of 3; " + self.s.progress_note())
        for i in range(3):
            body = self.s.body(self.prefill_messages(ctx, self.nonce(), "In one sentence, what is this file about?"), 64)
            r = self.s.chat_adapting(body, timeout=900) if i == 0 else self.s.chat(body, timeout=900)
            if not r.ok:
                self.say("✗", "First request", "failed", (r.error or "")[:160])
                self.invalid(f"the server did not complete the first request: {r.error}")
                return False
            self.check_cold(r, "first request")
            times.append(r.total)
        later = med(times[1:])
        pen = times[0] / later if later else None
        self.res["once"]["first_request"] = {"totals_s": [round(x, 3) for x in times], "penalty": None if pen is None else round(pen, 2)}
        note = f"a 1K-token prompt and a short answer; then {fmt_s(later)}: {pen:.1f}x on the first" if pen else ""
        if self.s.dropped_fields:
            note += f"  (server rejected sampling fields: {', '.join(self.s.dropped_fields)})"
            self.flag(f"sampling: the server rejected {', '.join(self.s.dropped_fields)}; not identical to Kapitsa bench")
        self.say("✓", "First request", fmt_s(times[0]), note)
        return True

    def p_thinking(self):
        self.step("Thinking", lambda: "is reasoning on by default, and does the switch turn it off?")
        msgs = [{"role": "user", "content": "Reply with exactly one word: what colour is a clear daytime sky?"}]
        plain = self.s.chat({"model": self.s.model, "messages": msgs, "max_tokens": 96, **self.s.sampling}, timeout=600)
        off = self.s.chat(self.s.body(msgs, 96), timeout=600)
        if not plain.ok or not off.ok:
            self.say("✗", "Thinking", "untested", ((plain.error if not plain.ok else off.error) or "")[:100])
            self.res["once"]["thinking"] = {"on_by_default": None, "switch_works": None, "error": plain.error or off.error}
            return
        default_on = bool(plain.reasoning.strip()) or bool(plain.reasoning_tokens) or "<think>" in plain.content
        leak = off.thinking_leak()
        share = None
        on = self.s.chat(self.s.body([{"role": "user", "content": "In two sentences, why do leaves change colour in autumn?"}], 4096 if default_on else 400,
                                     think="default" if default_on else True), timeout=1800)
        can_turn_on = on.ok and bool(on.thinking_leak())
        if on.ok and on.completion_tokens and on.thinking_leak():
            rt = on.reasoning_tokens if on.reasoning_tokens is not None else (len(on.reasoning) / max(1, len(on.reasoning) + len(on.content))) * on.completion_tokens
            share = rt / on.completion_tokens
        self.s.thinks_by_default = default_on
        self.s.think_switchable = (not leak) if default_on else None
        self.res["once"]["thinking"] = {"on_by_default": default_on, "switch": self.s.no_think_style, "switch_works": not leak if default_on else None,
                                        "can_turn_on": can_turn_on, "leak": leak, "reasoning_share_when_on": None if share is None else round(share, 3)}
        sw = {"both": "enable_thinking=false / reasoning_effort=none", "kwargs": "chat_template_kwargs.enable_thinking=false",
              "effort": "reasoning_effort=none"}[self.s.no_think_style]
        share_note = (f"; {share:.0%} of output is reasoning at the server's default" if default_on else f"; {share:.0%} of output is reasoning when on") if share is not None else ""
        if not default_on:
            self.say("✓", "Thinking", "off", ("off by default; the switch turns it on" if can_turn_on else "the model does not reason") + share_note)
        elif not leak:
            self.say("✓", "Thinking", "off by switch", f"on by default; {sw} turns it off{share_note}")
        else:
            self.say("!", "Thinking", "cannot switch off", "; ".join(leak) + share_note)
            self.flag("thinking: cannot be switched off; every row includes reasoning time")

    def p_warmup(self):
        """bench.py warmup: a ~2.9K prompt and a tool step, both untimed rows."""
        ctx = self.context(WARM_TOKENS, offset=WARM_OFFSET_CHARS)
        self.step("Warming up")
        r = self.s.chat(self.s.body(self.prefill_messages(ctx, self.nonce("warmup"), WARMUP_QUESTION), self.answer_tokens(256)), timeout=900)
        t = self.s.chat(self.s.body([{"role": "system", "content": SYS}, {"role": "user", "content": "Open src/main.py and tell me what the entry point does."}],
                                    self.answer_tokens(512), tools=TOOLS, tool_choice="auto"), timeout=900)
        self.res["once"]["warmup"] = {"prompt": r.brief(), "tool": t.brief()}
        self.check_cold(r, "warm-up")
        self.say("✓" if r.ok else "!", "Warmed up", fmt_s(r.total + t.total), f"a {fmt_k(r.prompt_tokens or WARM_TOKENS)}-token prompt and a tool call" if r.ok else (r.error or "")[:100])

    def p_writing(self):
        """bench.py t_decode: the LRU-cache module, 512 tokens, middle-window rate, median of N; sampled and greedy."""
        runs = 3 if self.a.quick else 5
        for greedy in (False, True):
            if greedy and self.a.quick:
                break
            rates, flags, drafts = [], [], []
            label = "Writing, greedy" if greedy else "Writing, T = 0.6"
            for k in range(runs):
                self.step(label, lambda k=k: f"run {k + 1} of {runs}; " + self.s.progress_note())
                msgs = [{"role": "system", "content": DECODE_SYS}, {"role": "user", "content": f"[run {random.random():.6f}] {DECODE_USER}"}]
                r = self.s.chat(self.s.body(msgs, DECODE_TOKENS, greedy=greedy), timeout=900)
                if not r.ok:
                    flags.append(f"run failed: {r.error}"[:120])
                    continue
                self.leak_check(r, "writing")
                rate = r.middle_rate()
                if rate:
                    rates.append(rate)
                elif r.rate_problem():
                    flags.append(r.rate_problem())
                if r.draft_acceptance() is not None:
                    drafts.append(r.draft_acceptance())
                d4 = distinct4(r.content or r.reasoning)
                if d4 is not None and d4 < REP_THRESHOLD:
                    flags.append(f"repetition loop (distinct-4-gram {d4})")
                if r.finish != "length":
                    flags.append(f"finish_reason={r.finish} (expected length)")
                if r.completion_tokens and r.completion_tokens != DECODE_TOKENS:
                    flags.append(f"completion_tokens={r.completion_tokens} != {DECODE_TOKENS}")
                self.mem_sample()
            key = "writing_greedy" if greedy else "writing"
            self.res["once"][key] = {"median_tps": med(rates), "min_tps": min(rates) if rates else None, "max_tps": max(rates) if rates else None,
                                     "valid_runs": len(rates), "runs": runs, "draft_acceptance": med(drafts), "flags": sorted(set(flags))}
            if rates:
                note = f"{min(rates):.1f}–{max(rates):.1f}, {len(rates)} of {runs} runs valid"
                if drafts:
                    note += f"; drafts accepted {med(drafts):.0%} (as the server reports)"
                self.say("✓", label, fmt_tps(med(rates)), note)
            else:
                self.say("✗", label, "failed", "; ".join(sorted(set(flags)))[:120])
            for f in sorted(set(flags)):
                if "repetition" in f or "usage" in f or "events" in f:
                    self.flag(f"writing: {f}")

    def rung_sizes(self, declared):
        sizes = [s for s in SIZES if int(s * CHARS_PER_TOKEN) + 2000 <= len(self.corpus)]
        if declared:
            sizes = [s for s in sizes if s <= declared * WINDOW_FRACTION] or sizes[:1]
        if self.a.quick:
            sizes = [s for s in sizes if s <= 16000]
        return sizes

    def p_ladder(self, sizes):
        budget_left = None if self.a.deep else LADDER_BUDGET_S
        last = None
        self.res["ladder_sizes"] = sizes
        for size in sizes:
            if size > 32000 and budget_left is not None:
                pf = (last or {}).get("prefill_tps")
                if not pf:
                    self.say("·", f"Reading {fmt_k(size)}", "skipped", "no reading rate measured at the previous rung; run with --deep")
                    self.res["depth"][str(size)] = {"skipped": "no reading rate at the previous rung"}
                    continue
                dec = (last or {}).get("write_tps") or 20
                mx = self.answer_tokens(256)
                reprefill = 0 if (last or {}).get("cache_hit") else 7 * size / pf
                predicted = size / pf * 1.25 + mx / dec + 3 * (1 + 2 + mx / dec) + DEPTH_WRITE_RUNS * DECODE_TOKENS / dec + STEP_TOKENS / pf + reprefill + 30
                if predicted > budget_left:
                    self.say("·", f"Reading {fmt_k(size)}", "skipped", f"predicted {predicted / 60:.0f} min at {pf:.0f} tok/s; run with --deep")
                    self.res["depth"][str(size)] = {"skipped": f"predicted {predicted:.0f} s over the {LADDER_BUDGET_S // 60}-min budget"}
                    continue
            t_start = time.monotonic()
            d = self.t_depth(size)
            self.res["depth"][str(size)] = d
            if budget_left is not None and size > 32000:
                budget_left -= time.monotonic() - t_start
            if d.get("failed") or self.server_down:
                break
            last = d

    def t_depth(self, size):
        """One rung: reading (cold, with planted facts), 3 follow-ups, writing at depth, agent step, recall, memory."""
        d = {"size": size, "protocol": "kapitsa prompt with 3 planted lines, same character count"}
        self.separator(f"{fmt_k(size)} context")
        sid = self.session_id()
        ctx = self.context(size, needles=True)
        msgs = self.prefill_messages(ctx, self.nonce(), RECALL_QUESTION)
        mx = self.answer_tokens(256)
        self.step(f"Reading {fmt_k(size)}", lambda: f"~{size:,} tokens sent; " + self.s.progress_note())
        r = self.s.chat(self.s.body(msgs, mx), timeout=3600, session=sid)
        d["reading"] = r.brief()
        if not r.ok:
            if r.status in REJECT_STATUSES:
                d["failed"] = "rejected"
                self.say("✓", f"Reading {fmt_k(size)}", "rejected", f"HTTP {r.status}: the window is below {fmt_k(size)}; the server refuses rather than truncates")
            elif r.status is None:
                d["failed"] = "timeout" if "timed out" in (r.error or "") else "connection"
                self.say("✗", f"Reading {fmt_k(size)}", "failed", (r.error or "")[:100])
                self.server_down = not self.health_check()
            else:
                d["failed"] = f"error {r.status}"
                self.say("✗", f"Reading {fmt_k(size)}", "failed", (r.error or "")[:100])
                if r.status >= 500:
                    self.server_down = not self.health_check()
            return d
        pt = r.prompt_tokens
        recall = self.recall_score(r.content)
        d.update({"prompt_tokens": pt, "ttft_s": r.ttft, "prefill_tps": (pt / r.ttft) if pt and r.ttft else None,
                  "cold_cached": r.cached_tokens, "recall": recall, "finish": r.finish, "answer": r.content[:400]})
        # how many prompt tokens this tokenizer makes of a target: fixed on the first rung, then every rung must match it
        if pt:
            if self.token_ratio is None:
                self.token_ratio = pt / size
                if size in REF_PROMPT_TOKENS and abs(pt - REF_PROMPT_TOKENS[size]) > 0.03 * REF_PROMPT_TOKENS[size]:
                    self.flag(f"reading {fmt_k(size)}: {pt} prompt tokens vs Kapitsa's reference {REF_PROMPT_TOKENS[size]} (different tokenizer or template; planted lines account for ~1%)")
            expected = size * self.token_ratio
            if pt < 0.8 * expected:
                d["failed"] = "truncated"
                self.say("!", f"Reading {fmt_k(size)}", "truncated", f"HTTP 200 but {pt:,} prompt tokens counted of ~{expected:,.0f} sent: the server cut the prompt and reported success")
                self.flag(f"window: silent truncation at {fmt_k(size)} (the server cuts the prompt and reports success)")
                return d
        self.check_cold(r, f"reading {fmt_k(size)}")
        self.leak_check(r, f"reading {fmt_k(size)}")
        if not pt:
            self.invalid("usage.prompt_tokens missing: reading rates cannot be computed")
        busy = self.busy_now()
        d["busy_after_read"] = busy
        self.say("✓" if not busy else "!", f"Reading {fmt_k(size)}", fmt_tps(d["prefill_tps"]),
                 (f"{pt:,} tokens in {fmt_s(r.ttft)}" if pt else "no usage in the response") + (f"; busy at the end of the read: {', '.join(busy)}" if busy else ""))
        if busy:
            self.flag(f"contention: {', '.join(busy)} during the {fmt_k(size)} read; that rate may be low")
        if r.finish != "stop":
            self.flag(f"reading {fmt_k(size)}: turn 1 finish_reason={r.finish} (some engines do not cache a turn cut by max_tokens)")
        # follow-ups: the agent pattern (append-only), 1 s apart; never substitute text for an empty answer (bench.py)
        fu = []
        if not (r.content or "").strip():
            d["followup_skipped"] = "turn 1 returned no content"
            self.say("·", f"Follow-up at {fmt_k(size)}", "not measured", "turn 1 returned no content; not substituting text")
            hist = msgs
        else:
            hist = msgs + [{"role": "assistant", "content": r.content}]
            prev_pt = pt
            for q in FOLLOWUPS:
                self.step(f"Follow-up at {fmt_k(size)}", lambda n=len(fu): f"turn {n + 1} of 3 on the same conversation; " + self.s.progress_note())
                time.sleep(1.0)
                m2 = hist + [{"role": "user", "content": q}]
                r2 = self.s.chat(self.s.body(m2, mx), timeout=3600, session=sid)
                if not r2.ok:
                    fu.append({"error": r2.error})
                    break
                self.leak_check(r2, f"follow-up at {fmt_k(size)}")
                c2 = r2.cached_tokens
                fu.append({"ttft_s": r2.ttft, "prompt_tokens": r2.prompt_tokens, "cached_tokens": c2, "finish": r2.finish,
                           "new_tps": ((r2.prompt_tokens - c2) / r2.ttft) if (r2.prompt_tokens and c2 is not None and r2.ttft and r2.prompt_tokens > c2) else None})
                if c2 is not None and prev_pt and c2 < 0.5 * prev_pt:
                    self.flag(f"follow-up at {fmt_k(size)} reused only {c2} of {prev_pt} prefix tokens")
                if not (r2.content or "").strip():
                    fu[-1]["note"] = "no content; series stopped"
                    break
                hist = m2 + [{"role": "assistant", "content": r2.content}]
                prev_pt = r2.prompt_tokens or prev_pt
        d["followups"] = fu
        fts = [x.get("ttft_s") for x in fu if x.get("ttft_s")]
        d["followup_ttft_s"] = med(fts)
        cached_vals = [x.get("cached_tokens") for x in fu if x.get("cached_tokens") is not None]
        d["followup_cached"] = med(cached_vals)
        d["cache_saving"] = (r.ttft / d["followup_ttft_s"]) if (r.ttft and d["followup_ttft_s"]) else None
        d["cache_hit"] = None
        if fts:
            timing_hit = r.ttft is not None and r.ttft >= 1.0 and d["followup_ttft_s"] <= 0.5 * r.ttft
            if cached_vals:
                d["cache_hit"] = bool(pt) and d["followup_cached"] >= 0.8 * pt
                basis = f"{d['followup_cached']:.0f} tokens reused"
                if not d["cache_hit"] and timing_hit:
                    self.flag(f"follow-up at {fmt_k(size)}: cached_tokens says {d['followup_cached']:.0f} but TTFT fell {d['cache_saving']:.0f}x; the cache field looks unreported")
                    d["cache_hit"] = True
                    basis += ", but TTFT says hit"
            else:
                d["cache_hit"] = timing_hit if r.ttft and r.ttft >= 1.0 else None
                basis = "inferred from timing; the endpoint reports no cached_tokens" if d["cache_hit"] is not None else "cold read too short to infer; no cached_tokens reported"
            verdict = {True: "cache hit, ", False: "NO cache hit, ", None: ""}[d["cache_hit"]]
            self.say("✓", f"Follow-up at {fmt_k(size)}", fmt_s(d["followup_ttft_s"]),
                     f"median of {len(fts)} turns; {verdict}{basis}" + (f"; {d['cache_saving']:.0f}x faster than the cold read" if d["cache_saving"] else ""))
            if d["cache_hit"] is False:
                self.flag(f"cache: no reuse at {fmt_k(size)} (every agent turn re-reads the conversation)")
        elif fu:
            self.say("✗", f"Follow-up at {fmt_k(size)}", "failed", (fu[0].get("error") or "")[:100])
        # writing at depth: the decode task on top of the cached conversation
        rates, early = [], 0
        n_runs = 2 if self.a.quick else DEPTH_WRITE_RUNS
        for k in range(n_runs + 2):  # up to two extra attempts when a run stops before the full answer
            if len(rates) >= n_runs:
                break
            self.step(f"Writing at {fmt_k(size)}", lambda k=k: f"run {len(rates) + 1} of {n_runs} on the cached conversation; " + self.s.progress_note())
            r3 = self.s.chat(self.s.body(hist + [{"role": "user", "content": f"[run {random.random():.6f}] {DEPTH_WRITE_USER}"}], DECODE_TOKENS), timeout=3600, session=sid)
            if r3.ok:
                self.leak_check(r3, f"writing at {fmt_k(size)}")
                if r3.finish == "length" and r3.middle_rate():
                    rates.append(r3.middle_rate())
                else:
                    early += 1  # bench.py flags a decode that does not run to max_tokens; a short answer gives no steady rate
            self.mem_sample()
        d["write_tps"] = med(rates)
        d["write_runs"], d["write_stopped_early"] = len(rates), early
        short = (self.res["once"].get("writing") or {}).get("median_tps")
        d["write_retention"] = (d["write_tps"] / short) if (d["write_tps"] and short) else None
        note = (f"{d['write_retention']:.0%} of the short-context rate, " if d["write_retention"] else "") + f"{len(rates)} of {n_runs} runs valid"
        if early:
            note += f"; {early} stopped before {DECODE_TOKENS} tokens and were not counted"
        self.say("✓" if len(rates) >= n_runs else "!" if rates else "✗", f"Writing at {fmt_k(size)}", fmt_tps(d["write_tps"]), note)
        # agent step: a tool result of STEP_TOKENS appended on the cached conversation, 64-token answer
        tool_out = self.context(STEP_TOKENS, offset=STEP_OFFSET_CHARS + SIZES.index(size) * 20000)
        self.step(f"Agent step at {fmt_k(size)}", lambda: f"a {fmt_k(STEP_TOKENS)}-token tool result appended; " + self.s.progress_note())
        r4 = self.s.chat(self.s.body(hist + [{"role": "user", "content": "Tool output:\n" + tool_out + "\n\nSummarise what that output shows in one sentence."}],
                                     self.answer_tokens(STEP_ANSWER)), timeout=3600, session=sid)
        if r4.ok:
            new = (r4.prompt_tokens or 0) - (r4.cached_tokens or 0)
            d["step"] = {"total_s": r4.total, "ttft_s": r4.ttft, "new_tokens": new, "cached_tokens": r4.cached_tokens,
                         "new_tps": (new / r4.ttft) if (new > 0 and r4.ttft and r4.cached_tokens is not None) else None, "answer_tokens": r4.completion_tokens}
            self.say("✓", f"Agent step at {fmt_k(size)}", fmt_s(r4.total),
                     f"a {fmt_k(STEP_TOKENS)}-token tool result" + (f" read at {fmt_tps(d['step']['new_tps'])}" if d["step"]["new_tps"] else "") + f", then {r4.completion_tokens or 0} tokens")
        else:
            d["step"] = {"error": r4.error}
            self.say("✗", f"Agent step at {fmt_k(size)}", "failed", (r4.error or "")[:100])
        # recall
        if not (r.content or "").strip():
            d["recall"] = None
            self.say("·", f"Recall at {fmt_k(size)}", "not answered", f"turn 1 returned no content (finish_reason={r.finish})")
        else:
            cut = " (answer cut by max_tokens)" if r.finish == "length" else ""
            self.say("✓" if recall == 3 else "!" if recall >= 1 else "✗", f"Recall at {fmt_k(size)}", f"{recall}/3", "three facts planted in the document, asked in the same request" + cut)
            if recall < 2 and not cut:
                self.flag(f"recall: {recall}/3 at {fmt_k(size)} (facts that are not literal matches get lost at this depth)")
        d["memory_bytes"] = self.mem_sample()
        d["memory_peak_bytes"] = self.peak_mem
        d["mapped_bytes"] = self.mapped_mem
        self.say("✓", f"Memory at {fmt_k(size)}", fmt_gb(d["memory_bytes"]), (f"now; {fmt_gb(self.peak_mem)} peak so far" + (f"; {fmt_gb(self.mapped_mem)} mapped files" if self.mapped_mem > 2**28 else "")) if d["memory_bytes"] else "footprint unavailable")
        return d

    def health_check(self):
        r = self.s.chat(self.s.body([{"role": "user", "content": "Say OK."}], 8), timeout=120)
        if not r.ok:
            self.say("✗", "Server health", "down", "the server stopped answering; the remaining rows are skipped")
            self.invalid("server stopped answering after an over-long prompt")
            return False
        return True

    def p_window(self, declared, sizes):
        depth = self.res["depth"]
        tested = [int(k) for k, v in depth.items() if not v.get("failed") and not v.get("skipped")]
        skipped = [int(k) for k, v in depth.items() if v.get("skipped")]
        top = max(tested) if tested else None
        failed = {int(k): v["failed"] for k, v in depth.items() if v.get("failed")}
        behaviour = None
        if failed:
            kinds = set(failed.values())
            behaviour = "rejects" if "rejected" in kinds else "truncates" if "truncated" in kinds else None
            if behaviour is None:
                self.flag(f"reading failed at {fmt_k(min(failed))}: {list(kinds)[0]}; not a window verdict")
        elif declared and declared <= 40000 and top and not self.a.quick:
            size = int(declared * 1.25)  # cheap on a small window: 1.25x the declared size, a fact at the front and one at the end
            if WINDOW_PROBE_OFFSET + int(size * CHARS_PER_TOKEN) < len(self.corpus):
                ctx = RECALL_NEEDLES[1][0] + "\n" + self.context(size, offset=WINDOW_PROBE_OFFSET) + "\n" + RECALL_NEEDLES[0][0]
                q = f"Answer in one line each: (a) {RECALL_NEEDLES[1][1]} (b) {RECALL_NEEDLES[0][1]}"
                r = self.s.chat(self.s.body(self.prefill_messages(ctx, self.nonce(), q), self.answer_tokens(96)), timeout=3600)
                expected = size * (self.token_ratio or 1.0)
                if r.status in REJECT_STATUSES:
                    behaviour = "rejects"
                elif r.ok and r.prompt_tokens and r.prompt_tokens < 0.8 * expected:
                    behaviour = "truncates"
                    self.flag(f"silent truncation: a {fmt_k(size)} prompt past the declared {fmt_k(declared)} window returned HTTP 200 with {r.prompt_tokens} tokens counted")
                elif r.ok and r.prompt_tokens:
                    behaviour = "accepts beyond declared"
                elif not r.ok:
                    behaviour = None
                    self.flag(f"window probe at {fmt_k(size)} failed: {(r.error or '')[:80]}")
                if not r.ok and r.status not in REJECT_STATUSES:
                    self.server_down = not self.health_check()
        self.res["window"] = {"declared": declared, "source": self.res.get("window_source"), "tested_to": top, "skipped": skipped, "past_window": behaviour}
        decl = f"{fmt_k(declared)} declared by /v1/models ({self.res.get('window_source')})" if declared else "not declared by /v1/models"
        past = {"rejects": "rejects prompts past it", "truncates": "TRUNCATES silently past it",
                "accepts beyond declared": "accepted a prompt past the declared size"}.get(behaviour, "not probed past the declared window" if declared else "not probed further")
        skip_note = f"; {', '.join(fmt_k(x) for x in skipped)} skipped for time" if skipped else ""
        self.say("!" if behaviour == "truncates" else "✓", "Window", f"tested to {fmt_k(top)}" if top else "untested", f"{decl}{skip_note}; {past}")

    def p_cache_survival(self):
        """Beyond the follow-up: a new question on the same prefix, and the prefix after another conversation."""
        d4 = self.res["depth"].get("4000") or {}
        if d4.get("failed") or not d4.get("prompt_tokens"):
            return
        ctx = self.context(4000, needles=True)
        nonce = self.nonce()
        base = self.prefill_messages(ctx, nonce, RECALL_QUESTION)
        sid = self.session_id()
        self.separator("once per run")
        self.step("Cache survival", lambda: "a new question on the same prefix, then another conversation")
        r1 = self.s.chat(self.s.body(base, self.answer_tokens(128)), timeout=1800, session=sid)
        if not r1.ok or not (r1.content or "").strip():
            self.say("·", "Cache survival", "untested", (r1.error or "cold turn returned no content")[:100])
            return
        self.check_cold(r1, "cache survival")
        pt = r1.prompt_tokens or d4["prompt_tokens"]

        def hit(r):
            if not r.ok:
                return None
            if r.cached_tokens is not None:
                return r.cached_tokens >= 0.8 * pt
            if r1.ttft and r1.ttft >= 1.0 and r.ttft is not None:
                return r.ttft <= 0.5 * r1.ttft
            return None
        r2 = self.s.chat(self.s.body(self.prefill_messages(ctx, nonce, FOLLOWUPS[0]), self.answer_tokens(128)), timeout=1800, session=self.session_id())
        other = self.prefill_messages(self.context(4000, offset=CACHE_SURVIVAL_OFFSET), self.nonce(), "In one sentence, what is this file about?")
        self.s.chat(self.s.body(other, 64), timeout=1800, session=self.session_id())
        r3 = self.s.chat(self.s.body(base + [{"role": "assistant", "content": r1.content}, {"role": "user", "content": FOLLOWUPS[1]}], self.answer_tokens(128)), timeout=1800, session=sid)
        nq, surv = hit(r2), hit(r3)
        inferred = r2.cached_tokens is None or r3.cached_tokens is None
        self.res["once"]["cache_survival"] = {"cold": r1.brief(), "new_question": r2.brief(), "after_other": r3.brief(),
                                              "new_question_hit": nq, "survives_other_conversation": surv, "inferred_from_timing": inferred}
        slots = (self.s.entry or {}).get("slots")
        if slots:
            self.s.extensions_used.append("slots")
        word = {True: "hit", False: "miss", None: "?"}
        self.say("✓" if (nq and surv) else "!" if (nq is False or surv is False) else "·", "Cache survival", f"{word[nq]} / {word[surv]}",
                 f"new question on the same prefix: {word[nq]}; prefix after another conversation: {'kept' if surv else 'lost' if surv is False else '?'}"
                 + (f" ({slots} slots)" if slots else "") + (" (inferred from timing)" if inferred else ""))
        if nq is False:
            self.flag("cache: a new question on a shared prefix misses (subagents and retries pay the full read)")

    def p_tools(self):
        trials, n, i = [], 10, 0
        mx = self.answer_tokens(400)
        while i < n:
            prompt, ok_names = TOOL_TRIALS[i % len(TOOL_TRIALS)]
            self.step("Tool calls", lambda i=i, n=n: f"trial {i + 1} of {n}: {TOOL_TRIALS[i % len(TOOL_TRIALS)][0][:50]}")
            r = self.s.chat(self.s.body([{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": prompt}], mx, tools=TOOLS), timeout=900)
            t = {"i": i, "prompt": prompt[:60], "ok": False, "why": "", "status": r.status, "finish": r.finish, "tool": r.tool_calls[0]["name"] if r.tool_calls else None}
            if not r.ok:
                t["why"] = f"request rejected: {(r.error or '')[:80]}" if r.status else (r.error or "request failed")[:80]
            else:
                t["why"] = tool_call_problem(r) or ""
                t["ok"] = not t["why"]
                if r.tool_calls:
                    t["expected_tool"] = r.tool_calls[0]["name"] in ok_names
            trials.append(t)
            i += 1
            if i == 10 and 1 <= sum(1 for x in trials if not x["ok"]) <= 2:
                n = 20
        passed = sum(1 for t in trials if t["ok"])
        rejected = [t for t in trials if t["status"] and t["status"] != 200]
        self.step("Tool calls", lambda: "round trip, forced choice, and a call with thinking on")
        m = [{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": "Read lines 10 to 20 of README.md and then tell me, in one sentence, what the file is about."}]
        r1 = self.s.chat(self.s.body(m, self.answer_tokens(300), tools=TOOLS), timeout=900)
        rt = None
        if r1.tool_calls:
            tc = r1.tool_calls[0]
            m += [{"role": "assistant", "content": r1.content or "", "tool_calls": [{"id": tc.get("id") or "call_1", "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}]},
                  {"role": "tool", "tool_call_id": tc.get("id") or "call_1", "content": "10: # benchcode\n11: benchcode measures what a coding agent would feel on a local model stack.\n12-20: (installation notes)"}]
            r2 = self.s.chat(self.s.body(m, self.answer_tokens(120), tools=TOOLS), timeout=900)
            rt = r2.ok and bool(r2.content.strip()) and not r2.tool_calls
        fc = self.s.chat(self.s.body([{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": "Hello there."}], self.answer_tokens(200), tools=TOOLS,
                                     tool_choice={"type": "function", "function": {"name": "glob"}}), timeout=900)
        forced = None if not fc.ok else (bool(fc.tool_calls) and fc.tool_calls[0]["name"] == "glob")
        think_tc = None
        if self.s.thinks_by_default or (self.res["once"].get("thinking") or {}).get("can_turn_on"):
            tk = self.s.chat(self.s.body([{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": TOOL_TRIALS[0][0]}], 4096, tools=TOOLS, think=True), timeout=1800)
            think_tc = "structured" if tk.tool_calls else ("cut off at 4096 tokens" if tk.finish == "length" else "NO call")
        self.res["once"]["tools"] = {"trials": trials, "passed": passed, "total": len(trials), "round_trip": rt, "forced_choice": forced, "tool_call_with_thinking_on": think_tc}
        if rejected and len(rejected) == len(trials):
            self.say("✗", "Tool calls", "rejected", f"every tool request failed with HTTP {rejected[0]['status']}: {rejected[0]['why'][:90]}")
            self.flag("tools: the server rejects tool requests")
            return
        why = sorted({t["why"] for t in trials if not t["ok"]})
        other = [t for t in trials if t["ok"] and t.get("expected_tool") is False]
        self.res["once"]["tools"]["other_tool_first"] = len(other)
        note = f"round trip {'ok' if rt else 'FAILED' if rt is False else 'no call to complete'}; forced choice {'honoured' if forced else 'ignored' if forced is False else 'rejected'}"
        if other:
            note += f"; {len(other)} chose another tool first ({', '.join(sorted({t['tool'] for t in other}))}), which is not an error"
        if think_tc is not None:
            note += f"; with thinking on: {think_tc}"
        if why:
            note += f"; misses: {'; '.join(why)}"[:160]
        self.say("✓" if passed >= 0.9 * len(trials) else "!", "Tool calls", f"{passed}/{len(trials)} valid", note)
        if passed < 0.9 * len(trials):
            self.flag(f"tools: only {passed}/{len(trials)} calls well-formed (malformed calls are dropped by the harness)")

    def p_session(self, declared, default=False):
        """A multi-step agent session with large tool results, then a compaction where it ends.

        The conversation is scripted (the assistant turns are ours, not the model's), so every server reads the same
        text: a large system prompt with the tool list, a task, then one big tool result per step. Each step is timed
        from request to the end of the model's reply, which should be the next tool call. The session stops after
        SESSION_STEPS, when the time budget is spent, or before the step that would pass WINDOW_FRACTION of the
        declared window, and the compaction is measured on that conversation: what a harness does when the context fills."""
        think = "default" if default else False
        tag = ", default" if default else ""
        key = "session_default" if default else "session"
        if not default:
            self.separator("agent session")
        depth = {int(k): v for k, v in self.res["depth"].items() if v.get("prefill_tps")}
        pf = depth[max(depth)]["prefill_tps"] if depth else None
        ratio = self.token_ratio or 1.0
        steps_planned = SESSION_STEPS
        if not self.a.deep and pf:
            start_cost = SESSION_PROMPT_TOKENS * ratio / pf
            step_cost = SESSION_RESULT_TOKENS * ratio / pf * 1.15 + 6
            steps_planned = max(3, min(SESSION_STEPS, int((SESSION_BUDGET_S - start_cost) / step_cost)))
        sid = self.session_id()
        system = {"role": "system", "content": f"[{self.nonce()}] {SESSION_SYSTEM}\n\n# Project notes and repository map\n{self.context(SESSION_PROMPT_TOKENS)}"}
        msgs = [system, {"role": "user", "content": SESSION_TASK}]
        if default:
            steps_planned = (self.res["once"].get("session") or {}).get("steps_planned") or steps_planned
        mx = DEFAULT_THINK_TOKENS if default else self.answer_tokens(128)
        ses = {"steps_planned": steps_planned, "result_tokens": SESSION_RESULT_TOKENS, "steps": [], "stopped": None, "thinking": "default" if default else "off"}
        self.res["once"][key] = ses
        self.step("Agent session" + tag, lambda: f"reading the {fmt_k(SESSION_PROMPT_TOKENS)} system prompt and tool list; " + self.s.progress_note())
        r0 = self.s.chat(self.s.body(msgs, mx, tools=TOOLS, think=think), timeout=3600, session=sid)
        if not r0.ok:
            self.say("✗", "Agent session" + tag, "failed", (r0.error or "")[:100])
            ses["stopped"] = f"start failed: {r0.error}"
            if r0.status is None or r0.status >= 500:
                self.server_down = not self.health_check()
            return
        self.check_cold(r0, "agent session start")
        ses["start"] = {"total_s": r0.total, "ttft_s": r0.ttft, "prompt_tokens": r0.prompt_tokens, "valid_call": tool_call_problem(r0) is None,
                        "reasoning_tokens": reasoning_count(r0), "finish": r0.finish}
        last_pt = r0.prompt_tokens or int(SESSION_PROMPT_TOKENS * ratio)
        t_session = r0.total
        for k in range(steps_planned):
            expect = last_pt + SESSION_RESULT_TOKENS * ratio + 100
            if declared and expect > declared * WINDOW_FRACTION:
                ses["stopped"] = f"the next step would pass {WINDOW_FRACTION:.0%} of the {fmt_k(declared)} window"
                break
            name, args = SESSION_CALLS[k % len(SESSION_CALLS)]
            result = self.context(SESSION_RESULT_TOKENS, offset=SESSION_RESULT_OFFSET + k * int(SESSION_RESULT_TOKENS * CHARS_PER_TOKEN))
            msgs = msgs + [{"role": "assistant", "content": "", "tool_calls": [{"id": f"call_{k + 1}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                           {"role": "tool", "tool_call_id": f"call_{k + 1}", "content": f"(output of {name}, part {k + 1})\n{result}"}]
            self.step("Agent session" + tag, lambda k=k: f"step {k + 1} of {steps_planned}: a {fmt_k(SESSION_RESULT_TOKENS)} tool result at ~{fmt_k(int(last_pt))} context; " + self.s.progress_note())
            r = self.s.chat(self.s.body(msgs, mx, tools=TOOLS, think=think), timeout=3600, session=sid)
            if not r.ok:
                ses["stopped"] = "rejected: the window is full" if r.status in REJECT_STATUSES else f"step {k + 1} failed: {(r.error or '')[:80]}"
                msgs = msgs[:-2]
                if r.status is None or r.status >= 500:
                    self.server_down = not self.health_check()
                break
            pt = r.prompt_tokens
            if pt and pt < 0.8 * expect:
                ses["stopped"] = f"step {k + 1} was truncated: {pt:,} prompt tokens counted of ~{expect:,.0f} sent"
                self.flag(f"window: silent truncation in the agent session at ~{fmt_k(int(expect))}")
                msgs = msgs[:-2]
                break
            cached = r.cached_tokens
            ses["steps"].append({"total_s": r.total, "ttft_s": r.ttft, "prompt_tokens": pt, "cached_tokens": cached,
                                 "new_tokens": (pt - cached) if (pt and cached is not None) else None,
                                 "cache_share": min(1.0, cached / last_pt) if (cached is not None and last_pt) else None,
                                 "problem": tool_call_problem(r), "reasoning_tokens": reasoning_count(r), "completion_tokens": r.completion_tokens, "finish": r.finish})
            if not default:
                self.leak_check(r, "agent session")
            t_session += r.total
            last_pt = pt or expect
            self.mem_sample()
            if self.server_down:
                break
        steps = ses["steps"]
        if not steps:
            self.say("·", "Agent session" + tag, "no steps", ses["stopped"] or "")
            return
        times = [s["total_s"] for s in steps]
        valid = sum(1 for s in steps if not s["problem"]) + (1 if ses["start"]["valid_call"] else 0)
        shares = [s["cache_share"] for s in steps if s["cache_share"] is not None]
        reasoning = [s["reasoning_tokens"] for s in steps if s.get("reasoning_tokens") is not None]
        cut = sum(1 for s in steps if s.get("finish") == "length")
        ses.update({"total_s": t_session, "median_step_s": med(times), "first_step_s": times[0], "last_step_s": times[-1],
                    "start_tokens": ses["start"]["prompt_tokens"], "end_tokens": int(last_pt), "valid_calls": valid, "replies": len(steps) + 1,
                    "median_cache_share": med(shares), "median_reasoning_tokens": med(reasoning) if reasoning else None, "steps_cut": cut})
        note = f"{len(steps)} steps of {fmt_k(SESSION_RESULT_TOKENS)} tool results, {fmt_k(ses['start_tokens'])} -> {fmt_k(int(last_pt))} context; the first read took {fmt_s(r0.total)}"
        if len(steps) < SESSION_STEPS:
            note += f"; {ses['stopped'] or 'fewer steps to fit the time budget'}"
        if default:
            off = self.res["once"].get("session") or {}
            note = f"no thinking switch sent; {note}"
            if off.get("median_step_s"):
                note += f"; {med(times) / off['median_step_s']:.1f}x the thinking-off step"
        self.say("✓", ("Session" if default else "Agent session") + tag, f"{t_session / 60:.1f} min" if t_session >= 90 else fmt_s(t_session), note)
        step_note = f"median; first {fmt_s(times[0])}, last {fmt_s(times[-1])}; "
        if default:
            step_note += (f"{med(reasoning):,.0f} reasoning tokens a step (median); " if reasoning else "") + (f"{cut} cut at {mx} tokens; " if cut else "")
        step_note += (f"{med(shares):.0%} of the prefix reused per step; " if shares else "the endpoint reports no cached_tokens; ") + f"{valid} of {len(steps) + 1} replies were well-formed tool calls"
        self.say("✓" if valid == len(steps) + 1 else "!", ("Step" if default else "Session step") + tag, fmt_s(med(times)), step_note)
        if shares and med(shares) < 0.8:
            self.flag(f"cache: only {med(shares):.0%} of the prefix reused per session step{tag} (each step re-reads earlier results)")
        if valid < len(steps) + 1:
            self.flag(f"tools: {len(steps) + 1 - valid} of {len(steps) + 1} session replies{tag} were not well-formed tool calls")
        if not self.server_down:
            self.p_compaction(system, msgs, int(last_pt), sid, default=default)

    def p_compaction(self, system, msgs, depth_tokens, sid, default=False):
        """What a harness does when the context fills: ask for a summary on the same conversation (same system prompt
        and tools, so the prefix stays cached), then start a new conversation from the summary."""
        think = "default" if default else False
        tag = ", default" if default else ""
        label = ("Compaction" + tag) if default else f"Compaction at {fmt_k(depth_tokens)}"
        mx = (2048 + DEFAULT_THINK_TOKENS) if default else max(2048, self.answer_tokens(2048))
        self.step(label, lambda: "the model summarises the conversation; " + self.s.progress_note())
        r = self.s.chat(self.s.body(msgs + [{"role": "user", "content": COMPACT_PROMPT}], mx, tools=TOOLS, think=think), timeout=3600, session=sid)
        comp = {"depth_tokens": depth_tokens, "thinking": "default" if default else "off"}
        self.res["once"]["compaction_default" if default else "compaction"] = comp
        if not r.ok:
            self.say("✗", label, "failed", (r.error or "")[:100])
            comp["error"] = r.error
            return
        summary = (r.content or "").strip()
        ct = max(0, (r.completion_tokens or 0) - (reasoning_count(r) or 0))
        write = r.middle_rate() or ((r.completion_tokens - 1) / (r.total - r.ttft) if (r.completion_tokens and r.ttft and r.total > r.ttft) else None)
        comp.update({"total_s": r.total, "ttft_s": r.ttft, "summary_tokens": ct, "summary_chars": len(summary), "write_tps": write,
                     "cached_tokens": r.cached_tokens, "prompt_tokens": r.prompt_tokens, "finish": r.finish, "called_tool": bool(r.tool_calls),
                     "reasoning_tokens": reasoning_count(r)})
        if not summary:
            self.say("!", label, "no summary", "the model called a tool instead of summarising" if r.tool_calls else f"empty reply (finish_reason={r.finish})")
            self.flag(f"compaction{tag}: the model did not produce a summary")
            return
        # the conversation after compaction: same system prompt, the summary, the task
        after = [system, {"role": "user", "content": "This session continues an earlier conversation that ran out of context. Summary of that conversation:\n\n"
                          + summary + "\n\nContinue with the task: " + SESSION_TASK}]
        self.step(label, lambda: "the first turn of the new conversation; " + self.s.progress_note())
        r2 = self.s.chat(self.s.body(after, DEFAULT_THINK_TOKENS if default else self.answer_tokens(128), tools=TOOLS, think=think), timeout=3600, session=self.session_id())
        if r2.ok:
            comp["next_turn"] = {"total_s": r2.total, "ttft_s": r2.ttft, "prompt_tokens": r2.prompt_tokens, "cached_tokens": r2.cached_tokens,
                                 "problem": tool_call_problem(r2), "reasoning_tokens": reasoning_count(r2)}
        note = (f"at {fmt_k(depth_tokens)} " if default else "") + f"where the session ended: a {ct:,}-token summary" + (f" written at {fmt_tps(write)}" if write else "") + f" after {fmt_s(r.ttft)}"
        if default and reasoning_count(r):
            note += f" and {reasoning_count(r):,} reasoning tokens"
        if r.cached_tokens is not None and r.prompt_tokens:
            note += f" ({r.cached_tokens / r.prompt_tokens:.0%} of the conversation cached)"
        if r.finish == "length":
            note += f"; cut at {mx} tokens"
        if r2.ok:
            c2 = r2.cached_tokens
            note += f"; next turn {fmt_s(r2.total)}" + (f" ({c2:,} of {r2.prompt_tokens:,} tokens cached)" if (c2 is not None and r2.prompt_tokens) else "")
        self.say("✓", label, fmt_s(r.total), note)
        if r.cached_tokens is not None and r.prompt_tokens and r.cached_tokens < 0.8 * r.prompt_tokens:
            self.flag(f"compaction{tag}: only {r.cached_tokens / r.prompt_tokens:.0%} of the conversation was cached when the summary was requested")

    def p_default_thinking(self, declared):
        """The agent rows again with no thinking switch sent: what a harness that leaves the server's default gets.
        Tool calls (10 trials), the agent session and its compaction. Skipped when the server does not think by default."""
        self.separator("default thinking")
        th = self.res["once"].get("thinking") or {}
        if not th.get("on_by_default"):
            self.say("·", "Default thinking", "same as above", "the server does not reason unless asked; the rows above are its default")
            self.res["once"]["default_thinking"] = {"skipped": "off by default"}
            return
        if self.s.always_reasons:
            self.say("·", "Default thinking", "same as above", "the model cannot be switched off; the rows above already include reasoning")
            self.res["once"]["default_thinking"] = {"skipped": "always reasons"}
            return
        self.res["once"]["default_thinking"] = {"switch_sent": None, "reply_cap": DEFAULT_THINK_TOKENS}
        trials = []
        for i in range(10):
            prompt, ok_names = TOOL_TRIALS[i % len(TOOL_TRIALS)]
            self.step("Tool calls, default", lambda i=i: f"trial {i + 1} of 10: {TOOL_TRIALS[i % len(TOOL_TRIALS)][0][:50]}")
            r = self.s.chat(self.s.body([{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": prompt}], DEFAULT_THINK_TOKENS, tools=TOOLS, think="default"), timeout=1800)
            why = (f"request rejected: {(r.error or '')[:80]}" if r.status else (r.error or "request failed")[:80]) if not r.ok else (tool_call_problem(r) or "")
            trials.append({"i": i, "ok": r.ok and not why, "why": why, "total_s": r.total, "reasoning_tokens": reasoning_count(r), "completion_tokens": r.completion_tokens,
                           "finish": r.finish, "tool": r.tool_calls[0]["name"] if r.tool_calls else None})
        passed = sum(1 for t in trials if t["ok"])
        times = [t["total_s"] for t in trials if t["total_s"]]
        reasoning = [t["reasoning_tokens"] for t in trials if t["reasoning_tokens"] is not None]
        cut = sum(1 for t in trials if t["finish"] == "length")
        self.res["once"]["tools_default"] = {"trials": trials, "passed": passed, "total": len(trials), "median_s": med(times) if times else None,
                                             "median_reasoning_tokens": med(reasoning) if reasoning else None, "cut": cut}
        off = self.res["once"].get("tools") or {}
        off_times = [t.get("total_s") for t in off.get("trials") or [] if t.get("total_s")]
        note = "no thinking switch sent"
        if times:
            note += f"; {fmt_s(med(times))} a call (median)" + (f", {med(times) / med(off_times):.1f}x the thinking-off call" if off_times else "")
        if reasoning:
            note += f"; {med(reasoning):,.0f} reasoning tokens a call (median)"
        if cut:
            note += f"; {cut} cut at {DEFAULT_THINK_TOKENS} tokens"
        why = sorted({t["why"] for t in trials if not t["ok"]})
        if why:
            note += f"; misses: {'; '.join(why)}"[:160]
        self.say("✓" if passed >= 9 else "!", "Tool calls, default", f"{passed}/{len(trials)} valid", note)
        if passed < 9:
            self.flag(f"tools: only {passed}/{len(trials)} calls well-formed with the server's default thinking")
        if not self.server_down:
            self.p_session(declared, default=True)

    def p_protocol(self):
        p = {}
        self.step("Protocol", lambda: "usage in streams, finish reasons, a truncated tool call")
        r = self.s.chat(self.s.body([{"role": "user", "content": "Say OK."}], 8), timeout=300)
        p["stream_usage"] = bool(r.usage and r.prompt_tokens)
        r2 = self.s.chat(self.s.body([{"role": "system", "content": SYS}, {"role": "user", "content": "Just say hello, no tools needed."}], self.answer_tokens(40), tools=TOOLS), timeout=600)
        p["plain_with_tools_finish"], p["plain_with_tools_called"] = r2.finish, bool(r2.tool_calls)
        r3 = self.s.chat(self.s.body([{"role": "user", "content": "Count from one to one hundred in words."}], 16), timeout=300)
        p["cap_finish"] = r3.finish
        r4 = self.s.chat(self.s.body([{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": TOOL_TRIALS[6][0]}], 40, tools=TOOLS), timeout=600)
        p["truncated_tool_call"] = {"status": r4.status, "finish": r4.finish, "tool_calls": len(r4.tool_calls), "error": r4.error,
                                    "content_chars": len(r4.content), "events": len(r4.ev_t)}
        cut = ("a partial call is streamed" if r4.tool_calls else "the cut text is streamed" if r4.content else "nothing is streamed") if r4.ok else "the request fails"
        self.res["once"]["protocol"] = p
        issues = []
        if not p["stream_usage"]:
            issues.append("no usage in streams")
        if r2.ok and r2.finish == "tool_calls" and not r2.tool_calls:
            issues.append("finish_reason=tool_calls on a plain answer")
        if r3.ok and r3.finish not in ("length", "max_tokens"):
            issues.append(f"cap reported {r3.finish!r}")
        if r4.status and r4.status >= 500:
            issues.append("500 on a truncated tool call")
        self.say("✓" if not issues else "!", "Protocol", "ok" if not issues else f"{len(issues)} issue" + ("s" if len(issues) > 1 else ""),
                 (f"usage in streams; finish reasons correct; when a tool call is cut by the output cap, {cut}") if not issues else "; ".join(issues))
        for i in issues:
            self.flag(f"protocol: {i}")

    def p_sustain(self, seconds):
        start = conditions(self.s.pids())
        rates, t0 = [], time.monotonic()
        self.step("Sustain", lambda: f"back-to-back writing, {max(0, seconds - (time.monotonic() - t0)):.0f} s left, run {len(rates) + 1}; " + self.s.progress_note())
        while time.monotonic() - t0 < seconds:
            msgs = [{"role": "system", "content": DECODE_SYS}, {"role": "user", "content": f"[run {random.random():.6f}] {DECODE_USER}"}]
            r = self.s.chat(self.s.body(msgs, DECODE_TOKENS), timeout=900)
            if not r.ok:
                break
            if r.middle_rate():
                rates.append(r.middle_rate())
        end = conditions(self.s.pids())
        res = {"seconds": seconds, "runs": len(rates), "thermal_start": start.get("thermal"), "thermal_end": end.get("thermal")}
        if len(rates) >= 3:
            k = max(1, len(rates) // 3)
            a, b = med(rates[:k]), med(rates[-k:])
            fall = (1 - b / a) * 100 if a else None
            res.update({"first_third_tps": a, "last_third_tps": b, "falloff_pct": fall})
            fresh = (self.res["once"].get("writing") or {}).get("median_tps")
            res["vs_fresh_pct"] = (b / fresh - 1) * 100 if fresh else None
            self.say("✓" if fall is None or fall < 10 else "!", "Sustain", f"{-fall:+.0f}%" if fall is not None else "–",
                     f"{a:.1f} -> {b:.1f} tok/s over {len(rates)} runs in {seconds // 60} min; thermal {start.get('thermal')} -> {end.get('thermal')}"
                     + (f"; {res['vs_fresh_pct']:+.0f}% against the first writing row ({fresh:.1f})" if fresh else ""))
            if res.get("vs_fresh_pct") is not None and res["vs_fresh_pct"] <= -10:
                self.flag(f"sustain: writing ended {-res['vs_fresh_pct']:.0f}% below the fresh rate; later rows ran on a slower machine than earlier ones")
            if fall is not None and fall >= 10:
                self.flag(f"sustain: writing fell {fall:.0f}% in {seconds // 60} min (thermal {end.get('thermal')})")
        else:
            self.say("·", "Sustain", "too few runs", f"{len(rates)} runs in {seconds} s")
        self.res["once"]["sustain"] = res

    def p_memory(self):
        now = self.mem_sample()
        idle = self.res.get("memory_idle_bytes")
        c0, c1 = self.res.get("conditions_start") or {}, self.res.get("conditions_end") or {}
        swap_growth = (c1.get("swap_used_bytes") or 0) - (c0.get("swap_used_bytes") or 0)
        self.res["once"]["memory"] = {"idle_bytes": idle, "peak_bytes": self.peak_mem, "end_bytes": now, "mapped_bytes": self.mapped_mem,
                                      "swap_growth_bytes": swap_growth}
        note = f"idle {fmt_gb(idle)} before the first request" + (f"; {fmt_gb(self.mapped_mem)} mapped files" if self.mapped_mem > 2**28 else "") + \
            "; includes every conversation the server still keeps" + (f"; swap grew {fmt_gb(swap_growth)}" if swap_growth > 2**28 else "")
        self.say("✓", "Memory", f"{fmt_gb(self.peak_mem)} peak", note)
        if swap_growth > 2**30:
            self.flag(f"memory: swap grew {fmt_gb(swap_growth)} (the model is squeezing other apps out)")

    # ---- summary
    def finish(self):
        self.res["summary"] = self.summary()
        os.makedirs(RESULTS_DIR, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        chip = re.sub(r"[^a-z0-9]+", "-", (self.res["machine"].get("chip_short") or "mac").lower())
        path = self.a.out or os.path.join(RESULTS_DIR, f"benchcode-{stamp}-{chip}-{re.sub(r'[^A-Za-z0-9.]+', '-', str(self.s.model))}.json")
        with open(path, "w") as f:
            json.dump(self.res, f, indent=1, default=str)
        self.res["result_file"] = path
        print(self.box(), file=self.out)
        proj = self.res["summary"].get("projected_task_min")
        if proj:
            print(f"  ≈ projection, not a measurement: a {proj['turns']}-turn agent task at {fmt_k(proj['at'])} context would take about {proj['minutes']} min "
                  f"(cold read + {proj['turns']} agent steps)", file=self.out)
        cold = self.res["summary"].get("cold_compaction_min")
        if cold:
            print(f"  ≈ projection, not a measurement: a harness that compacts with a different system prompt misses the cache and re-reads the "
                  f"{fmt_k(cold['depth_tokens'])} conversation first: about {cold['minutes']} min at the {fmt_k(cold['from_rung'])} reading rate", file=self.out)
        for fl in self.res["invalid"]:
            print(f"  {self.term.mark('✗')} {self.term.paint(31, 'INVALID')}: {fl}", file=self.out)
        for fl in self.res["flags"]:
            print(f"  {self.term.mark('!')} {fl}", file=self.out)
        print(f"  {self.term.dim('›')} Saved {path}", file=self.out)
        if self.a.json:
            print(json.dumps(self.res, indent=1, default=str))
        return 0

    def summary(self):
        o = self.res["once"]
        depth = {int(k): v for k, v in self.res["depth"].items() if not v.get("skipped") and not v.get("failed")}
        sizes = sorted(depth)
        s = {"writing_tps": (o.get("writing") or {}).get("median_tps"), "writing_greedy_tps": (o.get("writing_greedy") or {}).get("median_tps"),
             "reading_tps": {str(k): depth[k].get("prefill_tps") for k in sizes},
             "followup_ttft_s": {str(k): depth[k].get("followup_ttft_s") for k in sizes},
             "cache_saving": {str(k): depth[k].get("cache_saving") for k in sizes},
             "writing_at_depth_tps": {str(k): depth[k].get("write_tps") for k in sizes},
             "agent_step_s": {str(k): (depth[k].get("step") or {}).get("total_s") for k in sizes},
             "recall": {str(k): depth[k].get("recall") for k in sizes},
             "window": self.res.get("window"), "tools": f"{(o.get('tools') or {}).get('passed')}/{(o.get('tools') or {}).get('total')}",
             "thinking": o.get("thinking"), "memory_peak_bytes": self.peak_mem}
        ses, comp = o.get("session") or {}, o.get("compaction") or {}
        s["session"] = {k: ses.get(k) for k in ("total_s", "median_step_s", "first_step_s", "last_step_s", "start_tokens", "end_tokens", "valid_calls", "replies")} if ses.get("steps") else None
        s["compaction"] = {k: comp.get(k) for k in ("depth_tokens", "total_s", "summary_tokens", "write_tps")} if comp.get("total_s") else None
        sd, cd, td = o.get("session_default") or {}, o.get("compaction_default") or {}, o.get("tools_default") or {}
        s["default_thinking"] = {"session": {k: sd.get(k) for k in ("total_s", "median_step_s", "median_reasoning_tokens", "valid_calls", "replies", "steps_cut")} if sd.get("steps") else None,
                                 "compaction": {k: cd.get(k) for k in ("depth_tokens", "total_s", "summary_tokens", "reasoning_tokens")} if cd.get("total_s") else None,
                                 "tools": {k: td.get(k) for k in ("passed", "total", "median_s", "median_reasoning_tokens", "cut")} if td else None,
                                 "skipped": (o.get("default_thinking") or {}).get("skipped")}
        if comp.get("total_s") and depth:
            near = min(depth, key=lambda k: abs(k - comp["depth_tokens"]))
            if depth[near].get("prefill_tps"):
                s["cold_compaction_min"] = {"depth_tokens": comp["depth_tokens"], "prefill_tps": depth[near]["prefill_tps"], "from_rung": near,
                                            "minutes": round(comp["depth_tokens"] / depth[near]["prefill_tps"] / 60, 1)}
        ref = 16000 if 16000 in depth else (sizes[-1] if sizes else None)
        if ref and (depth[ref].get("step") or {}).get("total_s") and depth[ref].get("ttft_s"):
            s["projected_task_min"] = {"at": ref, "turns": 12, "minutes": round((depth[ref]["ttft_s"] + 12 * depth[ref]["step"]["total_s"]) / 60, 1)}
        return s

    def box(self):
        o, s, m = self.res["once"], self.res["summary"], self.res["machine"]
        e = self.s.entry or {}
        W = 76
        lines = [""]

        def row(label, text):
            text = text if len(text) <= W - 15 else text[:W - 16] + "…"
            lines.append(f"  │  {label:<11}{text:<{W - 15}}│")
        title = f"{self.s.model}" + (f"  ·  {e.get('quant')}" if e.get("quant") else "")
        title = title if len(title) <= W - 19 else title[:W - 20] + "…"
        mode = "quick run" if self.a.quick else "deep run" if self.a.deep else "full run"
        lines.append("  ╭" + "─" * (W - 2) + "╮")
        lines.append(f"  │  {title:<{W - 17}}● {mode:<10}│")
        lines.append(f"  │  {(m.get('name') or m.get('hw_model') or '') + '  ·  ' + (m.get('chip_short') or '') + ' ' + str(m.get('gpu_cores') or '?') + '-core GPU  ·  ' + fmt_gb(m.get('memsize_bytes')):<{W - 5}}│")
        lines.append("  │" + " " * (W - 2) + "│")
        depth = {int(k): v for k, v in self.res["depth"].items() if not v.get("skipped") and not v.get("failed")}
        sizes = sorted(depth)
        if s.get("writing_tps"):
            wr = f"{fmt_tps(s.get('writing_tps'))}  T = 0.6"
            if s.get("writing_greedy_tps"):
                wr += f"   ·   {fmt_tps(s['writing_greedy_tps'])}  greedy"
            row("Writing", wr)
        elif self.res["invalid"]:
            row("Stopped", "before any speed was measured; see INVALID below")
        if sizes:
            def num(v, nd=1):
                return "–" if v is None else (f"{v:.0f}" if v >= 100 else f"{v:.{nd}f}")
            cw = 9

            def trow(label, cells, unit=""):
                row(label, "".join(f"{c:>{cw}}" for c in cells) + (f"  {unit}" if unit else ""))
            lines.append("  │" + " " * (W - 2) + "│")
            trow("", [fmt_k(k) for k in sizes], "context")
            trow("Reading", [num(depth[k].get("prefill_tps")) for k in sizes], "tok/s")
            trow("Follow-up", [num(depth[k].get("followup_ttft_s"), 2) for k in sizes], "s")
            trow("Writing", [num(depth[k].get("write_tps")) for k in sizes], "tok/s")
            trow("Agent step", [num((depth[k].get("step") or {}).get("total_s")) for k in sizes], "s")
            trow("Recall", [f"{depth[k]['recall']}/3" if depth[k].get("recall") is not None else "–" for k in sizes])
            trow("Memory", [f"{depth[k]['memory_bytes'] / 2**30:.1f}" if depth[k].get("memory_bytes") else "–" for k in sizes], "GB")
            lines.append("  │" + " " * (W - 2) + "│")
        wnd = self.res.get("window") or {}
        if wnd:
            row("Window", (f"{fmt_k(wnd['declared'])} declared" if wnd.get("declared") else "not declared") + (f"  ·  tested to {fmt_k(wnd['tested_to'])}" if wnd.get("tested_to") else "") +
                (f"  ·  {', '.join(fmt_k(x) for x in wnd['skipped'])} skipped" if wnd.get("skipped") else "") +
                (f"  ·  {wnd['past_window']} past it" if wnd.get("past_window") else ""))
        ses, comp = o.get("session") or {}, o.get("compaction") or {}
        if ses.get("steps"):
            row("Session", f"{len(ses['steps'])} x {fmt_k(ses['result_tokens'])} results  ·  {fmt_s(ses['median_step_s'])} a step  ·  {ses['total_s'] / 60:.1f} min  ·  {fmt_k(ses['start_tokens'])} -> {fmt_k(ses['end_tokens'])}")
        elif self.a.quick:
            row("Session", "skipped (--quick)")
        if comp.get("total_s"):
            nt = (comp.get("next_turn") or {}).get("total_s")
            row("Compaction", f"{fmt_s(comp['total_s'])} at {fmt_k(comp['depth_tokens'])}  ·  {comp['summary_tokens']:,}-token summary" + (f"  ·  next turn {fmt_s(nt)}" if nt else ""))
        sd, cd, td = o.get("session_default") or {}, o.get("compaction_default") or {}, o.get("tools_default") or {}
        if sd.get("steps") or cd.get("total_s") or td:
            parts = []
            if sd.get("steps"):
                parts.append(f"{fmt_s(sd['median_step_s'])} a step  ·  {sd['total_s'] / 60:.1f} min" + (f"  ·  {sd['median_reasoning_tokens']:,.0f} reasoning tokens a step" if sd.get("median_reasoning_tokens") else ""))
            if cd.get("total_s"):
                ntd = (cd.get("next_turn") or {}).get("total_s")
                parts.append(f"compaction {fmt_s(cd['total_s'])}" + (f", next turn {fmt_s(ntd)}" if ntd else ""))
            if td:
                parts.append(f"tool calls {td['passed']}/{td['total']}" + (f" at {fmt_s(td['median_s'])}" if td.get("median_s") else ""))
            row("Default", "thinking as served, no switch sent:")
            row("", "  ·  ".join(parts))
        elif (o.get("default_thinking") or {}).get("skipped"):
            row("Default", f"thinking: same as above ({o['default_thinking']['skipped']})")
        cs = o.get("cache_survival") or {}
        hits = [depth[k].get("cache_hit") for k in sizes]
        fu = "hit" if hits and all(h is True for h in hits) else "MISS" if any(h is False for h in hits) else "?"
        if cs:
            w = {True: "hit", False: "miss", None: "?"}
            row("Cache", f"follow-up {fu}  ·  new question {w[cs.get('new_question_hit')]}  ·  {'survives others' if cs.get('survives_other_conversation') else 'lost to others' if cs.get('survives_other_conversation') is False else 'others: ?'}")
        tl = o.get("tools") or {}
        if tl:
            row("Tool calls", f"{s.get('tools')} valid" + f"  ·  round trip {'ok' if tl.get('round_trip') else 'failed' if tl.get('round_trip') is False else '–'}")
        th = o.get("thinking") or {}
        if th:
            row("Thinking", "untested" if th.get("on_by_default") is None else "off" if not th.get("on_by_default") else ("on by default, switched off" if th.get("switch_works") else "on, cannot be switched off"))
        row("Memory", f"{fmt_gb(self.peak_mem)} peak" + (f" + {fmt_gb(self.mapped_mem)} mapped" if self.mapped_mem > 2**28 else "") + (f"  ·  idle {fmt_gb(self.res.get('memory_idle_bytes'))}" if self.res.get("memory_idle_bytes") else ""))
        su = o.get("sustain") or {}
        if su.get("falloff_pct") is not None:
            row("Sustain", f"{-su['falloff_pct']:+.0f}% over {su['seconds'] // 60} min  ·  thermal {su.get('thermal_start')} -> {su.get('thermal_end')}")
        elif self.a.quick:
            row("Sustain", "skipped (--quick)")
        lines.append("  │" + " " * (W - 2) + "│")
        c = self.res.get("conditions_end") or self.res.get("conditions_start") or {}
        row("Conditions", f"{c.get('power')} power{(' ' + str(c.get('battery_pct')) + '%') if c.get('battery_pct') is not None else ''}  ·  {c.get('power_mode')} power mode  ·  thermal {c.get('thermal')}")
        quiet = (o.get("quiet") or {})
        top = sorted(quiet.items(), key=lambda x: -x[1])[:2]
        row("", (f"busy: {', '.join(f'{k[:16]} {v}%' for k, v in top)}" if quiet else "no other process over 20% CPU")
            + f"  ·  elapsed {round((self.res.get('elapsed_s') or (time.monotonic() - self.t0)) / 60, 1)} min")
        lines.append("  ╰" + "─" * (W - 2) + "╯")
        return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="benchcode", description=__doc__.split("\n\n")[0])
    ap.add_argument("server", nargs="?", default="http://127.0.0.1:8000", help="server URL (default http://127.0.0.1:8000)")
    ap.add_argument("--model", default=None, help="model id as listed by /v1/models (default: the active or first one)")
    ap.add_argument("--quick", action="store_true", help="4K and 16K only, fewer runs, no greedy, no sustain")
    ap.add_argument("--deep", action="store_true", help="every depth the window allows, no time budget, 5-min sustain")
    ap.add_argument("--json", action="store_true", help="print the result as JSON on stdout (progress on stderr)")
    ap.add_argument("--out", default=None, help="where to save the result JSON")
    args = ap.parse_args(argv)
    if args.quick and args.deep:
        ap.error("--quick and --deep are exclusive")
    # a background process inherits SIGINT ignored from a non-interactive shell, and Python then keeps it ignored:
    # install our own handlers so an interrupt or a plain kill both end the run with the partial row saved
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return Bench(args).run()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
