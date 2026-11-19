# NemoWispr roadmap

## Where it stands (Nov 15, 2026)

**Dictation works.** It takes ~0.5–1 s end to end, scores 9/10 on the synthetic clips, and doesn't clip the first words.

**The agent handles simple single-app tasks** and is honest when it fails, because a read-back check overrides its claims. The confirmation dialog has caught real mistakes. It's still weak at:
- nuance across windows;
- complex apps (Outlook works, but costs several screen reads per step);
- apps with no text fields (Calculator).

Two hard limits shape everything below:
- **Bolt is a 4B model.** In NVIDIA's own figures, Qwen 3.5 4B scores 75.1 to Bolt's 55.1 on Tau2-Bench, an agent benchmark.
- **The GPU budget.** Bolt with DFlash uses ~84% of 12 GB, and the speed cliff is at ~92%. There's no room for a second GPU model next to it.

## Decisions (Nov 15)

| Question | Decision |
|---|---|
| Repo visibility | ~~Private for now~~ Public alongside the LinkedIn post, after a personal-data audit (Nov 16) |
| Speech recognition | Evaluate NVIDIA's NeMo speech stack (Phase 2) |
| Apps for adapters | Outlook, Zen browser, Obsidian, Notepad |
| Calculator keypad | A per-app exception to the "typing needs a text field" rule |
| Start with Windows | Not now; later |
| Name (Nov 16) | NemoWispr, repo `hersh-gupta/nemowispr` |
| BF16 weights (Nov 16) | Skipped. The BF16 GGUF is 14 GiB, ~8.7 GiB of it in VRAM vs ~4.8 for Q8_K_XL, so it only fits under the 92% cliff without DFlash and at 32K context, for ~3–4× slower generation. Q8_K_XL is near-lossless. |
| Test suite (Nov 16) | Trim only: "send, declined" moved behind `-Full`; the default run is 10 checks |
| Control panel (Nov 16) | A Tk window from the tray in NVIDIA green, with Start/Stop/Restart Bolt behind code gates; result cards restyled to match |
| Model selection (Nov 16) | Launch profiles in the panel: every Bolt GGUF on disk × DFlash on/off, with estimated VRAM; `run-bolt.ps1 -Model`. Bolt only (audio encoder, drafter) |

## Principles to keep

1. **Measure before tuning.** Changing prompts based on one example has repeatedly traded one failure for another.
2. **Every new capability gets a code gate.** The confirmations and read-backs are the product while the model is small.
3. **Local only.** Audio and screen contents never leave the machine.
4. **Test against the sandbox,** never against real Notepad or Outlook.

## Phase 1: make "smarter" measurable

There's no score for the agent yet, only anecdotes. Do this first, because it tells us which of the later options is worth doing.

1. **Turn the suite into a benchmark.** `tests/run-tests.ps1` covers 10 checks by default (startup smoke, the dictation self-test, and 8 agent checks; `-Full` adds "send, declined"). Add scenarios from the real failures, rebuilt in the sandbox:
   - "add more to that list" (session memory);
   - "open X" when X is already open;
   - an Outlook-like compose form with placeholders and a cursor that doesn't follow `SetFocus`;
   - a Calculator-like keypad;
   - casual remarks ("that's fine") that must not cause actions.

   Record success, steps, seconds, false-success count and gates per task. Run each case 3×, because tool order varies.
2. **Experiment: thinking on.** The agent runs with `reasoning_effort: none` and conversational mode on. Try `low`/`medium`, with conversational mode on and off. It's a one-line change, and it's the cheapest shot at "not smart enough". Cost: latency per step.
3. **Replay set from real use.** `history.jsonl` holds the real commands. Build a dry-run replay that scores the agent's plans against these. Keep the set outside git, or anonymised, because it's personal.

## Phase 2: hearing, with NVIDIA's speech stack

**What fits this laptop.** NVIDIA's [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) is the official C++ runtime built on ggml (Apache-2.0). It has a native Windows installer (`irm …/install.ps1 | iex`) and CPU, CUDA and Vulkan builds. It runs **Nemotron 3.5 ASR Streaming 0.6B** (`nemo-speech pull nemotron-3.5`), which:
- handles 40+ languages;
- streams from 80 ms to 1 s of latency;
- can be served with `nemo-speech serve`, which offers an OpenAI-compatible transcription subset and a realtime WebSocket.

On the **CPU it takes no graphics memory**, which matters here because Bolt already uses 84%. A Q8_0 0.6B model is ~0.65 GB. Streaming means the text can be ready the moment the hotkey is released, instead of transcribing afterwards. sherpa-onnx (`pip install sherpa-onnx`) is a pure-Python fallback with official int8 packages for the same model.

Things not yet known:
- **CPU speed of the multilingual model.** NVIDIA's "6× real time on CPU" figure is for the English-only Nemotron-Speech Streaming 0.6B. The only CPU number for the 3.5 model is a third-party ONNX engine's 3.8×. Measure it.
- **Punctuation and capitalisation.** No source says whether the streaming model does them. Bolt's clean-up pass adds them either way.
- **Port clash.** `nemo-speech serve` defaults to **port 8080, which is Bolt's port**. Run it on another port (e.g. 8090).

**What doesn't fit.** [Nemotron 3.5 Transcribe](https://research.nvidia.com/labs/nemotron/NVIDIA-Nemotron-3.5-Transcribe/) (30B-A4B, 34B in total with its encoder) is the best open English recogniser, at a mean WER of 4.71%. NVIDIA lists native Windows support only on 32–96 GB cards (RTX 5090 / PRO 6000). It does natively two things built by hand here:
- **"written-form" transcription,** which is the clean-up pass;
- **"context terms",** which is the personal vocabulary item below.

It's the upgrade path if the hardware changes.

Steps:
1. **Collect a real-mic eval set.** Add an opt-in "keep audio for eval" toggle and gather 30–50 real dictations. Today only text is kept.
2. **Install NeMo-Speech.cpp** (CPU build first), run `nemo-speech serve --asr-model nemotron-3.5` on a port other than 8080, and add it to NemoWispr as a second recogniser behind a setting.
3. **Compare** it with Bolt's audio input on the eval set: accuracy, the time from release to text, and CPU load. Then try streaming during the hold over the WebSocket.
4. **Decide the architecture.** If NeMo wins, Bolt only does clean-up and the agent, both text-only, so the language model becomes swappable (see Phase 3.4). Keep the projector while `describe_screen` uses vision.
5. **Personal vocabulary:** names, jargon and product names. Pass them to clean-up, and to the recogniser if it supports biasing.
6. **Then retune the clean-up prompt** on the real-mic set. The known failure is the dropped "Thanks.".

## Phase 3: agent capability

1. **Adapters for Outlook, Zen, Obsidian and Notepad.** A 4B model plans badly across 10+ steps in a complex UI, so give it deterministic tools that use each app's own entry points instead of clicking where possible. Every adapter keeps the same gates. **Done (Nov 19):** `open_file`, `browser_url`, `summarize_page` and `draft_email` (Outlook through `mailto:`). Ideas still worth taking from Windows-MCP (MIT): reading web pages as text through the browser, filling several fields in one step, and waiting for an app or element to appear.
   - **Outlook:** a `mailto:to?subject=…&body=…` link opens a filled-in compose window and never sends by itself. Sending stays a clicked, confirmed step.
   - **Zen browser** (Firefox-based): pass a URL on `zen.exe`'s command line to open it, or use Ctrl+L (already a safe key) and paste, for search and navigation.
   - **Obsidian:** `obsidian://new` / `obsidian://open` links, or plain file writes into the vault folder for "add this to my note". Obsidian is an Electron app, so first check whether its interface shows up to UI Automation at all before planning anything that clicks.
   - **Notepad:** new-tab and append-to-current-tab helpers, aware of Notepad's tabs.
   - Each gets a sandbox scenario before it touches the real app.
2. **The Calculator exception.** Allow digits and operators (`0-9 . + - * / = ( ) %`, Enter, Backspace, Esc) as keystrokes when the target is Calculator and nothing else. It needs a keypad sandbox scenario, plus one proving the exception doesn't extend to other apps.
3. **Faster, better screen reading.**
   - Ask UI Automation for all the needed properties in one round trip (a cache request) instead of one call per property.
   - Group elements by region (dialog, form, toolbar).
   - Put the element under the cursor first.
   - Keep the tree between steps when nothing changed.

   Today Outlook costs a 4-second read per step, and the model calls `find` on almost every step.
4. **Model options, once Phase 1 gives a score.**
   - Thinking on.
   - Qwen 3.5 4B, if speech recognition moves off Bolt.
   - A larger MoE with experts offloaded to the CPU.

   Each costs VRAM, so check with `llama-fit-params` first; the 92% rule applies.
5. **Vision fallback with numbered boxes** (set-of-marks) for apps whose accessibility tree is poor. Currently `describe_screen` can only describe, not point.
6. **Undo as a feature.** Every replaced text is already stored. "Undo that" could restore the last replacement directly, rather than relying on the app's Ctrl+Z.
7. **Plan, then act.** For tasks with more than three steps, the model writes a short plan, and the code checks each step against it.

## Phase 4: user experience

- **Save settings.** Tray toggles reset when the app restarts; store them in `settings.json` in the data folder.
- **Card buttons:** Stop while working, Undo after a replacement, and a list of the steps taken.
- **Configurable hotkeys.**
- **Pin the tray icon.** It's hidden under ^ by default, and the pin resets whenever the Python environment path changes, so explain how to pin it the first time the app runs.
- **A better voice, if you want spoken results back.** NeMo-Speech.cpp also ships MagpieTTS (357M), which should sound far better than the Windows voice. Measure its CPU cost.
- **Start with Windows: later.** Add a Startup shortcut when you ask for it.
- **History controls:** how long history is kept, and a "delete my history" option.

## Phase 5: engineering

- **Move the data folder** from `%USERPROFILE%\.local\dictate` to `…\.local\nemowispr`, with a one-time startup migration. Rename the `BOLT_DICTATE_*` environment variables, keeping the old names as fallbacks.
- **Split the scripts** (`nemowispr.py` is ~1,300 lines) into modules (`asr`, `cleanup`, `agent/`, `ui/overlay`, `win32`) while keeping `uv run`.
- **pytest unit tests** for the pure parts: leaked-call parser, stop/reset phrases, clean-up validation, `Session`. Add GitHub Actions on `windows-latest` for those. GPU and UI tests stay local.
- **Pick a source of truth for the launcher.** `scripts/run-bolt.ps1` is a copy of `%USERPROFILE%\.local\llm\run-bolt.ps1`.
- **Move hard-coded settings** (server URL, model alias, timeouts) into the settings file.

## Next session (2–3 hours)

1. Start Bolt, then run `.\tests\run-tests.ps1` for a baseline (10 min).
2. The Calculator exception, with its two sandbox scenarios (20 min).
3. Thinking on/off on the suite (30 min). This decides how much Phase 3.4 matters.
4. Install NeMo-Speech.cpp and serve `nemotron-3.5` on port 8090. Add the "keep audio for eval" toggle and start collecting real dictations (45 min). The comparison waits until there are 30+ clips.
5. Settings persistence (30 min).
6. The first adapter: Outlook through `mailto:`, with a sandbox stand-in (45 min).
