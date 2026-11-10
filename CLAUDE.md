# benchcode

benchcode is a command you point at a local model server. It measures what a coding agent would feel
on that stack (model + runtime + hardware) and prints the numbers Kapitsa bench prints, plus the ones
it does not: speed and cache behaviour at every context depth the window allows, recall at depth, the
real window and what happens past it, tool-call fidelity, thinking mode, protocol conformance.

One line for a stranger: "How fast, how deep, how reliable is this local model for agent work? Ten
minutes, one table."

## Scope
- One command: `python3 benchcode.py [server-url]`; flags only `--model`, `--quick`, `--deep`, `--json`, `--out`.
- benchcode talks to the server's OpenAI-compatible endpoint (`/v1/models`, `/v1/chat/completions`)
  and to macOS. Nothing in it depends on which engine is behind the endpoint. The engine's process
  name appears in the header only because `lsof` reports it. The context window is read from the
  `/v1/models` entry when the server declares one there; otherwise it is found behaviourally.
- Thinking: every speed row and the first agent session run with thinking switched off, as in
  Kapitsa bench, so stacks do the same work. The default-thinking rows send no switch at all and
  report reasoning tokens per reply, because most harnesses send none and the Qwen3.8 templates
  default to the highest effort.
- Protocol: where a row also exists in Kapitsa bench (writing, reading, follow-up, sustain,
  memory, conditions) benchcode follows `bench/harness/bench.py`: the frozen corpus in `corpus/`,
  the same slices, a random nonce right after the system tag on every cold prompt, explicit
  sampling (T 0.6, top_p 0.95, top_k 20), thinking off with both known switches, a per-conversation
  `X-Session-Id`, the middle-window decode rate, median of 5 writing runs, the same validity rules
  (a cold prompt with more than 16 cached tokens, or reasoning returned with thinking off, is
  INVALID). One deliberate deviation: the reading prompt carries three planted lines for the recall
  row and is shortened by their length, so it has the same character count as Kapitsa's but is not
  byte-identical; its token count is checked against Kapitsa's reference within 3%. Tool calls,
  recall, agent step, window and cache survival are benchcode's own rows.
- Rows, in output order: first request, thinking, warm-up, writing (sampled and greedy), then per
  depth 4K, 16K, 32K, 64K, 128K as the window and a time budget allow: reading, follow-up with cache
  saving, writing at depth, agent step (a 4K tool result then 64 tokens), recall of three planted
  facts, memory; then window (declared, tested usable to, rejects or truncates past it), cache
  survival (new question on the same prefix; prefix after another conversation), tool calls
  (well-formed structured calls out of 20, round trip, forced choice, with thinking on), protocol
  (usage in streams, finish reasons, truncated tool call), agent session (a 12K system prompt with
  the tool list, then up to 8 scripted steps that each append an 8K tool result: time per step,
  total, prefix reused per step, well-formed replies), compaction (on the session's conversation,
  where it ended: the summary request, then the first turn of the conversation rebuilt from the
  summary), then the same agent rows with no thinking switch sent (tool calls, agent session,
  compaction: what a harness that leaves the server's default gets; skipped when the server does
  not reason by default), sustain, memory, conditions. A summary box, then INVALID reasons and
  flags, then the saved JSON path.
- benchcode is not an agent and does not choose a compaction policy. The session grows until its
  steps are done or the next step would pass three quarters of the declared window, and compaction
  is measured there. Tool results are text; images are out of scope.
- `--quick`: 4K and 16K, 3 writing runs, no greedy, no agent session, compaction or default-thinking rows, no sustain. Default: up to 32K always, 64K and
  128K when the measured reading speed predicts they fit a 15-minute budget. `--deep`: every depth,
  no budget, 5-minute sustain.
- Output: the table on stdout, one JSON row per run in `results/`.

## Rules that are not negotiable
- No engine-specific logic. If a number cannot be had through `/v1` or macOS, it is not measured,
  and the row says so.
- A cold prompt that reports more than 16 cached tokens makes the run INVALID (the nonce was not
  honoured, or the server caches across it). Reasoning returned when thinking was switched off is
  INVALID unless the model cannot be switched off at all. Low Power Mode stops the run.
- Nothing is extrapolated inside the table. The two projections (minutes for a 12-turn task; the
  re-read a cache-missing compaction would cost) are printed below the box and labelled as such.
- Every row names the model and quantization as `/v1/models` lists them, the declared and tested
  window, thinking state, sampling as sent, macOS version, machine, power source and mode, thermal
  state, swap, busy processes, corpus sha256, date.
- Probes never change server state they cannot restore: no model switches, no option changes.
- The runner works unattended: no prompts, no interactive steps, exit code 0 on completion.
- Anything the stack did that surprised us goes in `FAILURES.md`. Each entry becomes a check.

## Not now
GUI, Windows, Linux, vision, computer use, a live agent task, engine-specific endpoints, starting
or restarting servers, a context sweep that reloads the model.

## Style
- Python 3.9+, a single file `benchcode.py`, standard library only. The corpus in `corpus/` is
  Kapitsa bench's frozen corpus (Apache-2.0 excerpts of Hugging Face Transformers, see
  `corpus/NOTICE.md`), unchanged.
- Small, readable, boring. Apache-2.0. No option that v0 does not need.
