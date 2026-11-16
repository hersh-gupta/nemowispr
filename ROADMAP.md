# NemoFlow roadmap

## Where it stands (Nov 15, 2026)

**Dictation works.** It takes ~0.5–1 s end to end, scores 9/10 on the synthetic clips, and doesn't clip the first words.

**The agent handles simple single-app tasks** and is honest when it fails, because a read-back check overrides its claims. The confirmation dialog has caught real mistakes. It's still weak at:
- nuance across windows;
- complex apps (Outlook works, but costs several screen reads per step);
- apps with no text fields (Calculator).

Two hard limits shape everything below:
- **Bolt is a 4B model.** In NVIDIA's own figures, Qwen 3.5 4B scores 75.1 to Bolt's 55.1 on Tau2-Bench, an agent benchmark.
- **The GPU budget.** Bolt with DFlash uses ~84% of 12 GB, and the speed cliff is at ~92%. There's no room for a second GPU model next to it.

## Principles to keep

1. **Measure before tuning.** Changing prompts based on one example has repeatedly traded one failure for another.
2. **Every new capability gets a code gate.** The confirmations and read-backs are the product while the model is small.
3. **Local only.** Audio and screen contents never leave the machine.
4. **Test against the sandbox,** never against real Notepad or Outlook.

## Phase 1: make "smarter" measurable

There's no score for the agent yet, only anecdotes. Do this first, because it tells us which of the later options is worth doing.

1. **Turn the suite into a benchmark.** `tests/run-tests.ps1` covers 12 checks today. Add scenarios from the real failures, rebuilt in the sandbox:
   - "add more to that list" (session memory);
   - "open X" when X is already open;
   - an Outlook-like compose form with placeholders and a cursor that doesn't follow `SetFocus`;
   - a Calculator-like keypad;
   - casual remarks ("that's fine") that must not cause actions.

   Record success, steps, seconds, false-success count and gates per task. Run each case 3×, because tool order varies.
2. **Experiment: thinking on.** The agent runs with `reasoning_effort: none` and conversational mode on. Try `low`/`medium`, with conversational mode on and off. It's a one-line change, and it's the cheapest shot at "not smart enough". Cost: latency per step.
3. **Replay set from real use.** `history.jsonl` holds the real commands. Build a dry-run replay that scores the agent's plans against these. Keep the set outside git, or anonymised, because it's personal.

## Phase 2: hearing (the dictation half)

1. **Collect a real-mic eval set.** Add an opt-in "keep audio for eval" toggle, and gather 30–50 real dictations. Today only text is kept.
2. **Compare dedicated speech recognisers with Bolt:**
   - NVIDIA Parakeet or whisper.cpp on the CPU (sherpa-onnx);
   - or OpenWhispr wholesale, an MIT-licensed, Windows-native app with local Whisper/Parakeet and no telemetry;
   - against Bolt's audio input, on accuracy, latency and VRAM.
3. **Decide the architecture.** If a CPU recogniser wins, Bolt no longer needs audio input:
   - its audio/vision projector can be dropped, freeing ~0.9 GB of VRAM, unless `describe_screen` still needs vision;
   - and the language model becomes swappable (see Phase 3.4).

   If you adopt OpenWhispr for dictation, NemoFlow becomes command-only. Add a tray toggle to turn off its dictation hotkey so the two don't collide.
4. **Personal vocabulary.** Names, jargon and product names passed to transcription and clean-up, like Wispr Flow's dictionary.
5. **Then retune the clean-up prompt** on the real-mic set. The known failure is the dropped "Thanks.".

## Phase 3: agent capability

1. **Adapters for the apps you actually use.** A 4B model plans badly across 10+ steps in a complex UI. Give it deterministic tools for frequent tasks, each with the same gates. For example:
   - `outlook_compose(to, subject, body)`, where sending still needs confirmation;
   - `notepad_new_tab(text)` / `notepad_append(text)`;
   - `browser_open(url)` / `browser_new_tab()`;
   - `calc(expression)`, which clicks the buttons by their developer ids.

   Pick the top 3–5 apps first (open decision below).
2. **Faster, better screen reading.**
   - Ask UI Automation for all the needed properties in one round trip (a cache request) instead of one call per property.
   - Group elements by region (dialog, form, toolbar).
   - Put the element under the cursor first.
   - Keep the tree between steps when nothing changed.

   Today Outlook costs a 4-second read per step, and the model calls `find` on almost every step.
3. **Vision fallback with numbered boxes** (set-of-marks) for apps whose accessibility tree is poor: Electron apps, canvas, games. Currently `describe_screen` can only describe, not point.
4. **Model options, once Phase 1 gives a score.**
   - Thinking on.
   - Qwen 3.5 4B, if the speech recogniser moves off Bolt.
   - A larger MoE with experts offloaded to the CPU.

   Each costs VRAM, so check with `llama-fit-params` first; the 92% rule applies.
5. **Undo as a feature.** Every replaced text is already stored. "Undo that" could restore the last replacement directly, rather than relying on the app's Ctrl+Z.
6. **Plan, then act.** For tasks with more than three steps, the model writes a short plan, and the code checks each step against it.

## Phase 4: user experience

- **Save settings.** Tray toggles reset when the app restarts; store them in `settings.json` in the data folder.
- **Card buttons:** Stop while working, Undo after a replacement, and a list of the steps taken.
- **Configurable hotkeys** (needed if OpenWhispr also runs).
- **Pin the tray icon.** It's hidden under ^ by default, so explain how to pin it the first time the app runs.
- **Start with Windows:** a Startup shortcut. Ask first.
- **History controls:** how long history is kept, and a "delete my history" option.

## Phase 5: engineering

- Rename `bolt_*` to `nemoflow`: files, tray title, mutex name, and log/data folder (with a migration).
- Split the 1,300-line scripts into modules (`asr`, `cleanup`, `agent/`, `ui/overlay`, `win32`) while keeping `uv run`.
- pytest unit tests for the pure parts: leaked-call parser, stop/reset phrases, clean-up validation, `Session`. Add GitHub Actions on `windows-latest` for those. GPU and UI tests stay local.
- `scripts/run-bolt.ps1` is a copy of `%USERPROFILE%\.local\llm\run-bolt.ps1`. Pick one as the source of truth.
- Move hard-coded settings (server URL, model alias, timeouts) into the settings file.

## Open decisions (yours)

1. Keep the repo private, or make it public, and under which license?
2. Keep Bolt for speech recognition, move to Parakeet/Whisper, or use OpenWhispr for dictation?
3. Which 3–5 apps should get adapters first?
4. Should the Calculator-style keyboard block become an adapter, or a per-app exception to the "typing needs a text field" rule?
5. Start NemoFlow with Windows?

## Suggested first session (2–3 hours)

1. Start Bolt, then run `.\tests\run-tests.ps1` for a baseline (10 min).
2. Thinking on/off on the suite (30 min). This decides how much Phase 3.4 matters.
3. Answer the open decisions, especially which apps matter (10 min).
4. Settings persistence and the rename (1 h).
5. The first adapter, probably Outlook compose, with a sandbox scenario that mimics it (1 h).
