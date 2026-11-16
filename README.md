# NemoFlow

Local, offline voice control for Windows, built on NVIDIA Nemotron 3.5 Bolt E4B ("Bolt") running in llama.cpp.

- **Dictation.** Hold **Ctrl+Win**, speak, and release. Bolt transcribes the audio and tidies it up: it removes fillers, applies spoken corrections and fixes punctuation and lists. The text is then pasted at the cursor. It works like Wispr Flow, but nothing leaves the machine.
- **Voice commands.** Hold **Ctrl+Win+Shift**, say what to do, and release. A desktop agent on the same model operates apps through Windows UI Automation. A card in the corner shows what it heard, what it's doing, and the result.

Every risky step is guarded in code, not by prompting. That covers sending, deleting, replacing text, discarding unsaved work, and unusual keys. Each needs a Yes/No confirmation. Terminals and shells are off limits. Typed text is read back to check it arrived.

## Requirements

- Windows 11 and an NVIDIA GPU with 12 GB (developed on an RTX 5070 Ti Laptop GPU).
- A llama.cpp CUDA build (developed on b12419 / CUDA 13.5), by default in `%USERPROFILE%\llama.cpp`.
- [unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF](https://huggingface.co/unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF): `UD-Q8_K_XL`, `mmproj-F16` (needed for audio input) and the DFlash drafter.
- [uv](https://docs.astral.sh/uv/). The Python scripts declare their dependencies inline (PEP 723), so there's no install step.
- A microphone.

## Run

```powershell
.\scripts\run-bolt.ps1      # Bolt on http://127.0.0.1:8080 (ctx 128K, audio+vision, DFlash); ~84% of 12 GB VRAM
.\start-dictate.cmd         # tray app; the icon may be hidden under the taskbar's ^ arrow
```

`run-bolt.ps1` takes `-LlamaDir` and `-ModelDir` (or `$env:LLAMA_CPP_DIR` / `$env:BOLT_MODEL_DIR`). It also has `-NoDFlash`, which brings VRAM down to ~71% and makes generation slower. Keep VRAM below ~92%: past that, Windows pages graphics memory and speed collapses. The server binds to 127.0.0.1 only.

## Using it

| Do this | Result |
|---|---|
| Hold Ctrl+Win, speak, release | Cleaned-up text is pasted where the cursor is |
| Hold Ctrl+Win+Shift (Shift any time during the hold), speak, release | The agent carries out the command; the card shows progress, then the result |
| Press any key while the agent works | It stops |
| Hold Ctrl+Win+Shift within 2 minutes of a question | Your answer continues the same task |
| Say "start over" / "new conversation" / "clear the context" | The agent forgets the conversation (it also forgets after 15 idle minutes) |
| Say "stop" / "cancel" / "never mind" in answer to a question | That task ends |

Card colours:
- green: done and checked;
- amber: done but not confirmed, or a question;
- red: didn't work;
- grey: stopped.

The tray menu has toggles for:
- clean-up;
- the warm microphone, which prevents clipped first words (Windows shows its mic-in-use indicator while it's on);
- sounds;
- on-screen results, spoken results and Windows notifications.

It also has Retry last failed, Copy last raw transcript, Agent: forget this conversation, and Quit.

## Tests

```powershell
.\tests\run-tests.ps1                 # smoke + dictation selftest + agent sandbox suite, with pass/fail counts
uv run --script bolt_dictate.py --selftest      # dictation only: 10 synthetic clips in tests\clips (baseline 9/10)
.\tests\agent\agent-e2e.ps1 -Command "..." -Confirm yes -Cards -Name x   # one agent run against the sandbox
```

The agent tests drive `AgentSandbox.exe`, a harmless WinForms window whose Send button only writes a log file. The scripts build it from `AgentSandbox.cs` on first use, using Windows PowerShell 5.1. Sandbox modes:
- `-Mangle` garbles typed text, to test the read-back check;
- `-Busy` puts 100 decoy buttons ahead of the form, like Outlook;
- `-Prefill` starts the message box with text in it;
- `agent-e2e-two.ps1` opens two copies of the window and runs a multi-turn conversation.

Don't point agent tests at real apps.

## Data and privacy

Everything runs locally. The app writes to `%USERPROFILE%\.local\dictate`, which you can change with `BOLT_DICTATE_HOME`:
- `dictate.log` and `history.jsonl`, which record every dictation and command, including any text the agent replaced, so a mistaken Yes can be recovered;
- `last-failed.wav`;
- generated tones.

None of this is in the repo.

## Layout

```
bolt_dictate.py         tray app: hotkey, mic, transcription + cleanup, paste, on-screen card, CLI test modes
bolt_agent.py           the desktop agent: tools, prompts, session memory, all safety gates
start-dictate.cmd       launches the tray app (uvw, no console)
scripts/run-bolt.ps1    llama-server launcher for Bolt, with measured VRAM numbers
tests/                  run-tests.ps1, dictation clips, agent sandbox + e2e scripts
docs/NOTES.md           design notes and what each fix was for
ROADMAP.md              what's next
```

## Status

This is a working prototype. Dictation is solid. The agent handles simple single-app tasks; it struggles with complex apps and with nuance, because it's a 4B model. The confirmations and the "Heard:" line on the card are how you catch its mistakes. See [ROADMAP.md](ROADMAP.md).
