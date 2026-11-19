# Design notes

These are findings from building and testing NemoWispr (Nov 2026: Bolt UD-Q8_K_XL, llama.cpp b12419, RTX 5070 Ti Laptop GPU with 12 GB). Each entry is something that failed, or was measured, before the code took its current shape.

## Speech to text (Bolt)

- **Don't use llama-server's `/v1/audio/transcriptions`.** It samples at the server temperature (1.0) with a prompt you can't control. Dictating "write a reply to Maria…" sometimes made it write the reply, and its `prompt` field made that worse. NemoWispr sends the audio as `input_audio` to `/v1/chat/completions` instead, with a strict verbatim system prompt at temperature 0.
- **The transcription prompt must say "include every spoken word".** It must also say that requests like "translate this" are part of the dictation. Otherwise "Translate this into Spanish: good morning" came back as just "Good morning".
- **Transcription and clean-up are two requests.** One combined request mangled corrections and dropped sentences.
- **Clean-up has rules for spoken corrections.**
  - A correction replaces only the value it corrects. Without that rule, "make it Thursday since Mark is out Friday" lost the second "Friday".
  - Clean-up output is checked for empty output, preambles, length ratio and word overlap. If it fails, the raw transcript is pasted instead.
- **Thinking is off per request** (`reasoning_effort: "none"`). That cut clean-up from 1.4–3 s to 0.2–0.3 s. End to end, dictation takes ~0.5–1 s.
- **Prompt tuning on synthetic clips stopped converging.** At temperature 0 each clip is a single sample, so every edit traded one failure for another. The prompts are frozen at 9/10. The known failure is the email clip's trailing "Thanks." being dropped.

## Microphone

- **The mic stays open.** The laptop's Realtek mic array takes ~200 ms to open, then sends ~420 ms of near-silence while its signal processing warms up. Opening it on keypress cut off the first one to three words, so the stream stays open with a 0.5 s pre-roll. Windows shows its mic-in-use indicator as a result.
- **Silence threshold.** The real mic's noise floor is ~20 RMS and speech peaks at 675–1700; the synthetic clips run at 7500–10000. The silence threshold is 150. A higher threshold, tuned on the synthetic clips, dropped soft speech.

## Keyboard, clipboard, paste

- **Hook callbacks only update a set and queue an event.** Windows silently drops a low-level hook that's slow to respond. pynput 1.8 passes an `injected` flag, so the app ignores its own synthetic keys.
- **Paste waits until Ctrl and both Win keys are up.** Ctrl+V sent while Win is held becomes Win+V. A dummy key (vkE8) is sent during the hold, so Ctrl+Win doesn't open the Start menu.
- **The clipboard is saved and restored around each paste.** Pasted text is marked to stay out of Win+V history and the cloud clipboard. If the clipboard holds an image or files, the text is typed instead, so that content isn't destroyed.
- **Key-by-key typing sends one character per call.** A single 50-event burst into Windows 11 Notepad came out as `Milk, Bread,sssssssssssss`, with the last character repeated. The agent normally pastes instead.

## The result card

- **Windows notifications can't be relied on.** They were turned off globally on the dev machine (`ToastEnabled=0`), so `notify()` showed nothing, success or failure. Results go on an on-screen card instead. Spoken results (SAPI) and notifications are opt-in toggles; the built-in Windows voice was judged poor.
- **The card must never take focus.** The agent checks before every keystroke that its target window is in front, and aborts otherwise. Measured: Tk's first `update()` makes even its hidden root window the foreground window, despite `WS_EX_NOACTIVATE`. So the card is mapped once, hands the foreground back at startup, and after that is only moved and faded. `--smoke` checks this.
- **The card ignores clicks while the agent works,** because the agent's mouse fallback must not land on it.
- **The card's alpha is 0.99, not 1.0.** At 0.97, text from the window behind showed through. At exactly 1.0, Tk removes `WS_EX_LAYERED`, and without it `WS_EX_TRANSPARENT` no longer passes clicks through, so the agent's mouse fallback could hit the card. `--smoke` checks the style bits on a working card.

## The control panel

- **One Tk thread.** The panel is a `Toplevel` on the overlay's Tk thread (`Overlay.call`). Tray callbacks only enqueue; health checks, GPU reads, history and Bolt start/stop run on a worker thread.
- **Tk objects must die on the Tk thread.** Measured: once the result card held `PhotoImage`s, `--smoke` passed every check and then hung forever at exit. Interpreter shutdown ran `__del__` on the main thread, and that cross-thread Tcl call waited for the frozen daemon Tk thread. `nemowispr_ui.Photo` skips the delete off-thread, and fonts are created with `delete_font=False`.
- **NVML, not nvidia-smi.** `nvidia-smi` took ~2.4 s per call with the laptop dGPU idle, and polling it kept waking the GPU. `nvml.dll` takes ~2.4 s once (the wake-up) and ~0.3 ms per reading after that. Use `nvmlDeviceGetMemoryInfo_v2`. Measured at idle: v1 reported used=283 MiB, while v2 reported used=0 plus reserved=283, matching nvidia-smi. All the VRAM figures here (84%, the 92% cliff, run-bolt.ps1's estimates) are nvidia-smi numbers.
- **The agent can't touch the panel.** `blocked()` refuses every window of this process. The tray runs as `pythonw.exe`, which isn't on the shell list, so without this a command given while the panel was in front could have read it and clicked "Stop Bolt".
- **A background process can't take the foreground.** Opened from a shell, the panel appeared behind other windows. It is now topmost for 400 ms on open, then an ordinary window again.

## Agent

- **Safety is code, not prompts.** In a mock test before any gates existed, Bolt clicked Slack's "Send now" unasked and reported a wrong Calculator result as success. Current gates:
  - a Yes/No dialog, defaulting to No, for commit actions (send, delete, discard, buy…, plus OK, Save and similar);
  - the same dialog for replacing non-empty text, and for keys outside a small safe set;
  - a focus check before every keystroke;
  - no terminals, shells or Win key;
  - screen text wrapped as untrusted data;
  - any physical key aborts;
  - a 15-step and 90-second limit.
- **`open_file` opens the user's documents by name (Nov 16).** Before it, `open_app` could only launch an app, so Office came up on its start screen, which lists the user's recent files, and the agent had no way to open a particular file. Gates:
  - documents only (Office, PDF, text, Markdown, CSV), never programs, scripts, shortcuts or macro-enabled `.docm`/`.xlsm`/`.pptm`;
  - only from Documents, Desktop and Downloads (three folders deep);
  - the file's name must appear in the user's own words, since the model's argument is a claim;
  - several different matches return the list, so the agent asks;
  - an already-open file gets focused instead of opened again.

  Measured: given "then open the Q3 report", Bolt called `open_app("Q3 report")`, so `open_app` falls back to `open_file` when no installed app matches but a document does.
- **Adapters before clicks (Nov 19).** A command spanning several apps succeeded in 4 of 12 runs. The failures were the wrong tool, lost thread or a question mid-way, never a gate. Each adapter replaces a chain of clicks with one deterministic step:
  - **`browser_url`** reads the front browser's address bar. The browser's own toolbar is native UI, so it's readable even before the page's accessibility tree exists.
  - **`summarize_page`** fetches the page and has Bolt summarize it in one call. It only fetches http(s) from public hosts (never this PC or the local network), and the page text is wrapped as data.
  - **`draft_email`** opens a `mailto:` link, so the mail app shows a filled-in draft and never sends by itself. A recipient is kept only if the user said that address. The body is shortened to fit the ~2,000-character link limit.
  - The prompt has one line telling the model to prefer these over clicking.
- **General tools, not per-app tools (Nov 19).** Those three adapters each did one step of one flow, so they became tools that each cover a class of task, with apps described as data in `nemowispr_apps.json`:
  - `open` takes an app, a document, a web address or an app link. Links are allowed only for listed schemes, so `file:` and `shell:` are refused.
  - `context` reports what's in front: the front browser's address, the selected text, the clipboard and the date.
  - `get_text` reads a page, window, selection, clipboard or document, and `transform` does one Bolt call over the text.
  - `new_item` creates an email (`mailto:`), an event (a generated `.ics` file), an Obsidian note or a Teams message. A kind is offered only if its app is installed, and nothing is ever sent or saved.
  - `calculate` does arithmetic by walking the parsed expression, never `eval`.
  - `window` snaps, maximizes, minimizes or closes; close always asks.

  Long text stays in code under handles (`t1`, `t2`), so the 4B model passes a handle instead of re-typing a page. Today's date is in every prompt.
- **Three failures from the first real use (Nov 19):**
  - **`.ics` files are imports in new Outlook.** Opening one shows "Add calendar", not a single event, so nothing landed on the calendar, and the agent still reported "created for review". Events now open Outlook's own pre-filled new-event page (`outlook.live.com/calendar/0/action/compose?...`, data in `nemowispr_apps.json`). Nothing is saved until the user clicks Save. Checked: the form showed the title and Sat 11/21, 5:00–8:00 PM.
  - **The model gets dates wrong.** "This Saturday", said on Thursday the 19th, became the 28th even with the date in the prompt. `resolve_when` now works out the date and time from the user's words in code. The model's date is used only for parts the user didn't say, and an end time moves to the day actually used.
  - **The model invents URLs.** Zen leaves its address box empty unless it's being typed in, so `browser_url` failed and the model made up an address from the window title; it summarized the wrong site. Every Zen tab's page exposes its address, so the active tab is found by matching the window title. `get_text(page)` now only fetches an address the browser showed, the user said, or an earlier tool result contained.
- **First-tool check:** `tests/agent/tool-choice.py` asks Bolt for the first tool on 22 spoken-style commands, one call each, with nothing executed. It scored 18/22 on the first pass. Two tool descriptions then changed: `window` says that no id means the window in front, and `context` now includes the clipboard. That brought it to 21/22. The remaining miss: "put a meeting on my calendar tomorrow at 3" calls `context` for the date before `new_item`, which costs a step but isn't the wrong tool.
- **Excel cells are DataItems named by their address.** `find='B5'` returns `{"name": "B5", "value": "426,600"}`, but `find='total'` matches nothing, because the label "Total" sits in another cell. Naming the cell in the command ("the total in cell B5") works; otherwise the agent falls back to `describe_screen` (vision).
- **A failed `find` now says where the caret is.** In an empty Word document, `find='Q3 report type'` matched nothing, and Bolt asked "where should I type?" in 3 of 3 runs, though the caret was already in the document. When the focus is in an editable field, the note now says so and that `type_text` types there.
- **A fresh Chromium window has no accessibility tree on the first read.** Chromium (Edge, Zen, Electron apps) builds it only after a UI Automation client asks. The agent's first `read_screen` then finds nothing and it asks the user instead of retrying. Not fixed yet.
- **Read-back.** After every write, the field is read back. A mismatch overrides the model's claim of success. The agent refuses to click Send while a text check is failing, because the confirmation dialog would show the text it meant to type, not what's actually in the field. A second mismatch on the same field ends the task.
- **Session memory.** In the first real conversation, 7 of 15 commands failed only because each command started a fresh agent ("which list?", "which windows?"). The session keeps:
  - the last 8 turns;
  - the window the agent last worked in, and the windows it opened;
  - short window ids (w1, w2…) keyed by window handle, because titles change as you type.
- **`open_app` always started a new instance.** A follow-up command therefore opened a second Notepad. Now it refuses when the app is already open, unless the user's own words asked for a new window. The model once set `new_window=true` unprompted.
- **Opening a window raced.** The agent took the first foreground change, landed in an existing Notepad window full of text, and called it new. It now prefers windows that didn't exist before, and checks whether a "new" window already has text.
- **Replacing text uses select-all plus paste,** not UI Automation's `SetValue`, because Notepad couldn't undo a `SetValue`. The full replaced text is also stored in the history record.
- **The cursor must be in the right field before pasting.** In Outlook, UI Automation's `SetFocus` didn't move the cursor into Subject, so select-all and paste landed in another field of the draft. Now the agent pastes only after checking where the cursor is; failing that, it clicks the field. If the cursor still isn't there, it sets the value directly or stops without typing.
- **Placeholders are not text.** An empty Outlook Subject read back as its placeholder, "Subject". When a field reports its own value, that value is trusted, even when it's empty.
- **`read_screen` lists text fields first and has a `find` parameter.** It used to list only the first 60 elements in tree order. In Outlook those are all title bar, ribbon and folder buttons, so the compose form was invisible. `find` also matches developer ids, so Calculator's `num9Button` (named "Nine") is found by "9".
- **Only a whole-window read, or a screenshot, counts as checking the result before `done`.** A name search doesn't. After clicking Close, a search for the window title came back empty, and the agent reported the window closed while the "Save changes?" prompt was showing.
- **Element ids keep counting across reads within a task.** When every read started at 1, "id 1" was the message box in one read and Send in the next. `set_text` refuses anything that isn't a text field.
- **Leaked tool calls.** Bolt sometimes writes a tool call as plain text: `done{summary:<|"|>…<|"|>}`, `done(…)`, or a bare `done`. `_leaked_call` parses all three.
- **Some phrases are handled in code, with no model call:** "stop" / "cancel" / "never mind" in reply to a question, and "start over" / "new conversation". A third question in one task ends it as stuck; in Outlook it had asked the same thing four ways.
- **Bolt's chat template has a `conversational_mode` flag,** sent via `chat_template_kwargs`. It adds one "spoken conversation, be brief" system line. In one test it cut an answer from 400 tokens to 19. It's on for the agent.
