# Working on NemoWispr

Windows-only. There are two single-file Python scripts with inline PEP 723 dependencies, run through uv, plus `nemowispr_ui.py`, which `nemowispr.py` imports from the same folder. There's no package or virtualenv to manage. Read `docs/NOTES.md` before changing behaviour: most of the code exists because something measurably failed without it.

## Rules

- **Safety lives in code, not prompts.** A 4B model ignores prompt-level caution. In testing it clicked "Send now" unasked, reported a wrong result as success, and called a garbled field "typed". A new capability that can change anything gets a code gate: a confirmation, a focus check, or a read-back. Never weaken a gate to make a test pass.
- **The model's arguments are claims; the user's words and the screen are facts.** Example: `new_window=true` is honoured only if the user's own words asked for a new window.
- **Test against the sandbox only.** The user's real apps hold real data: Notepad has unsaved drafts, and Outlook is live mail. For unit calls into `nemowispr_agent`, use `dry_run=True` when a real window is involved.
- **Don't start the Bolt server or other long-running processes unless asked.** Give the command instead. Bolt plus DFlash uses ~84% of 12 GB VRAM, and past ~92% Windows pages VRAM and everything crawls.
- **Restarting the tray app** (when the user asks): stop the old `pythonw` running `nemowispr.py` first, because a single-instance mutex makes a second copy exit. Start it detached so it outlives the tool's process tree, with `Invoke-CimMethod Win32_Process Create` on `uvw run --gui-script <repo>\nemowispr.py`. Then confirm the "started" line in `%USERPROFILE%\.local\dictate\dictate.log`.
- **The dictation prompts are frozen at 9/10 on synthetic clips.** Edits there trade one failure for another. Tune only against real-mic data.
- **Never commit runtime data.** `history.jsonl` holds the user's dictations; the `.gitignore` covers it.

## Commands

```powershell
uv run --script nemowispr.py --smoke          # builds the tray app's parts; includes "card never takes focus"
uv run --script nemowispr.py --selftest       # dictation, tests\clips (9/10 baseline)
uv run --script nemowispr.py --agent "..." --dry-run          # plan against the real screen, change nothing
uv run --script nemowispr.py --panel [--shot x.png]   # control panel alone (--shot: screenshot it, exit)
uv run --script nemowispr.py --card-demo <dir>         # every result-card kind, screenshotted into <dir>
.\tests\run-tests.ps1 [-Full]                    # 10 checks (-Full: 11); agent cases take focus for a few minutes
```

## Code map

- `nemowispr.py`
  - `process`: transcribe (chat with `input_audio`, temperature 0), clean up, validate.
  - `Recorder`: warm mic with 0.5 s pre-roll.
  - `Clipboard` / `deliver`: paste, preserving the user's clipboard.
  - `Overlay`: Tk card on its own thread; `call()` runs the control panel on that same thread.
  - `App`: hook, control loop, job loop, tray.
  - `run_agent`: session, resume, and the reset/stop phrases.
  - `main`: CLI test modes.
- `nemowispr_agent.py`
  - `Session`: memory across commands, window ids.
  - `Agent._loop`: one tool call per step, leaked-call parsing.
  - Tool handlers:
    - `_read_screen`: text fields first, `find`, ids unique per task;
    - `_set_text` / `_type_text`: focus verified, read back;
    - `_click`: commit gate;
    - `_press_keys`: deny by default;
    - `_open_file` / `find_documents`: documents only, from Documents/Desktop/Downloads, and every name word must be the user's;
    - adapters: `_browser_url` (address bar), `_summarize_page` (public http(s) only), `_draft_email` (mailto, never sends; recipients only if said).
  - `_confirm`: MessageBox dialog; the full replaced text goes into the record.
  - `blocked`: shells, consoles, and every window of this process (the panel's Stop/Forget aren't for the model).
- `nemowispr_ui.py`
  - Palette (`GREEN` #76B900 etc.), `KIND` colours, Pillow artwork (supersampled), `tray_icon`.
  - `Photo` / `delete_font=False`: Tk objects only delete themselves on the Tk thread (exit hang otherwise).
  - `gpu` (NVML), `health`, `props`, `start_bolt`, `stop_bolt` (port owner must be llama-server.exe).
  - `launch_profiles`: Bolt GGUFs on disk × DFlash, VRAM estimates with run-bolt.ps1's constants (keep them in step).
  - `Panel`: worker thread polls; `render` maps state onto canvas items; two-click gates on Start/Stop/Restart; the profile popover is canvas items (no extra window).
