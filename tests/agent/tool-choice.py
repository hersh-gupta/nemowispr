"""
Which tool does the agent pick FIRST for a spoken-style command? One Bolt call per command, nothing executed,
no window touched: it measures the failure that mattered (wrong tool / lost thread), cheaply. Needs Bolt on :8080.

  uv run --with uiautomation --with pillow python tests/agent/tool-choice.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import nemowispr_agent as ag   # noqa: E402

CASES = [   # (command, acceptable first tools)
    ("open the Q3 report", {"open"}),
    ("open Excel", {"open"}),
    ("go to github.com", {"open"}),
    ("open my bluetooth settings", {"open"}),
    ("what page am I on", {"context"}),
    ("summarize the page I have open", {"context", "get_text"}),
    ("get the link from my browser, summarize the page, and draft an email with the summary", {"context", "get_text"}),
    ("what's 15 percent of 426,600", {"calculate"}),
    ("add up 128,400, 141,900 and 156,300", {"calculate"}),
    ("draft an email to maria@example.com saying I'll be ten minutes late", {"new_item"}),
    ("put a meeting with Priya on my calendar tomorrow at 3", {"new_item"}),
    ("make a note called groceries with milk and eggs", {"new_item"}),
    ("send a Teams message to sam@example.com asking about lunch", {"new_item"}),
    ("translate the text I selected into Spanish", {"get_text", "context"}),
    ("read the Q3 report and tell me the total", {"get_text", "open"}),
    ("what's on my clipboard", {"get_text", "context"}),
    ("put this window on the left half of the screen", {"window"}),
    ("maximize this window", {"window"}),
    ("close this window", {"window"}),
    ("which windows are open", {"list_windows"}),
    ("type hello world here", {"type_text"}),
    ("click the Save button", {"read_screen"}),
]


def first_tool(agent, command):
    agent.msgs = [{"role": "system", "content": ag.SYSTEM_PROMPT + f"\nNow: {datetime.now():%A %d %B %Y, %H:%M}."},
                  {"role": "user", "content": command}]
    m = agent._chat(agent.msgs)
    calls = m.get("tool_calls") or ([c] if (c := ag._leaked_call(m.get("content"))) else [])
    if not calls:
        return "(no tool)", ""
    f = calls[0]["function"]
    return f["name"], f.get("arguments", "")


def main():
    agent = ag.Agent("http://127.0.0.1:8080", "bolt", object, dry_run=True)
    ok = 0
    for cmd, want in CASES:
        name, args = first_tool(agent, cmd)
        hit = name in want
        ok += hit
        print(f"{'ok  ' if hit else 'MISS'} {name:13} {cmd[:60]:60} {'' if hit else '(want ' + '/'.join(sorted(want)) + ')'}"
              f"  {str(args)[:70] if not hit else ''}")
    print(f"\n{ok}/{len(CASES)} first tools as expected")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    with ag.com_thread():
        sys.exit(main())
