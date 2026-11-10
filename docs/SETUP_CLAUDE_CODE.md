# Claude Code by voice: setup and test on the laptop

Task brief for the coding agent with shell access to the laptop. Same rules as
[SETUP_WINDOWS.md](SETUP_WINDOWS.md): do not edit source code, do not push, never
handle API keys or login tokens. Do this after, or together with,
[RETEST_RUN3.md](RETEST_RUN3.md).

## What this is

FRIDAY passes a spoken task to Claude Code and reads back the outcome. She does
none of the work herself.

1. Owner: "Friday, ask Claude Code to ..."
2. FRIDAY reads the task back and asks "Shall I send it?"
3. Owner: "yes" (or "no", or more words to add to the task)
4. Claude Code runs in the background in one configured folder. The owner can
   keep talking to FRIDAY.
5. When it finishes, FRIDAY says a one or two sentence summary. The full output is
   saved in `%APPDATA%\FRIDAY\claude-code\`.

It has been run against the real Claude Code program on Linux only. **Windows and
real speech are untested**; finding out is the point of this brief.

## Stage 1: Claude Code itself

```powershell
claude --version
```

If it is missing, the owner installs Claude Code and signs in themselves, in their
own terminal. Do not handle the sign-in.

Then, in the folder the owner chooses as the project folder (start with a small or
throwaway project, not anything important):

```powershell
"Reply with the single word pong" | claude -p --output-format json
```

**Check 1:** the output is one JSON object whose `result` contains "pong". If it
says not logged in, stop and ask the owner to sign in.

Note in the report what `(Get-Command claude).Source` prints (an `.exe` or a
`.cmd` file): it matters if stage 3 fails to start the program.

## Stage 2: config

Add to `%APPDATA%\FRIDAY\config.toml` (forward slashes in the path):

```toml
[claude_code]
enabled = true
project_dir = "C:/Users/<owner>/Projects/<project>"
permission_mode = "plan"
keep_session = "day"
```

`keep_session` decides how long every task goes into the same Claude Code chat:
`"day"` (default) until midnight, even if FRIDAY is closed and reopened; `"run"`
until FRIDAY is closed; `"always"` until the owner says "Friday, new Claude Code
chat". To give access to several projects, `project_dir` may be the folder that
contains them all; the owner then names the project in the task.

Permission levels, to be chosen by the owner, not by the agent:

| Value | What Claude Code may do |
|---|---|
| `"plan"` | Read the code and propose changes. Changes nothing. Start here. |
| `""` | Whatever Claude Code's own settings for that folder allow. |
| `"acceptEdits"` | Change files in that folder. |

**Check 2:** `friday providers` still runs without a config error.

## Stage 3: text first

`friday chat` is interactive, so the owner types:

1. `ask Claude Code to tell me what this project does` → FRIDAY shows the task and
   asks whether to send it.
2. `yes` → "Sent. I'll tell you when Claude Code is done."
3. Wait. A line in square brackets appears with the summary and the log path.
   (In text chat it appears when it is ready, even while the prompt is waiting.)

**Check 3:** the summary makes sense and the log file exists and contains the full
answer. If it fails with "could not start Claude Code", record the exact message.

## Stage 4: by voice (owner present, earbuds on)

`friday voice`, then:

- **A. Send.** "Friday, ask Claude Code to tell me what this project does." She
  reads it back; say "yes"; she confirms; later she speaks the outcome unprompted.
- **B. Mishearing.** Write down exactly what she read back each time. How often was
  the task wrong? How was "Claude Code" transcribed (the `you>` line)? If the
  phrase was not recognised at all, she answers as a normal chat turn: record the
  `you>` line, the spelling is needed to fix it.
- **C. Decline.** Give a task, say "no": nothing is sent.
- **D. Add more.** Give half a task, wait for the read-back, then say the rest
  (three words or more): she reads back the combined task.
- **E. Keep talking.** After a "yes", ask FRIDAY an ordinary question while Claude
  Code is working: she answers, and the result is announced afterwards without
  cutting into her own sentence or the owner's.
- **F. Follow-up.** "Ask Claude Code what did I just ask you about?" → its answer
  shows it remembers the first task.
- **F2. Same chat after a restart.** Close `friday voice`, start it again, and
  repeat F: it should still remember the first task. Then say "Friday, new Claude
  Code chat" and repeat F once more: now it should not.
- **G. Cancel.** Give a long task ("ask Claude Code to review every file in
  detail"), confirm, then say "cancel Claude Code": she says it is stopped and no
  result is announced later.
- **H. Only if the owner chose `acceptEdits`:** a small real change ("add a line
  to the README saying hello"), then check the file.

## Report

Append a `# Claude Code by voice` section to `backend\SETUP_REPORT.md`:

1. Claude Code version, and whether `claude` is an `.exe` or `.cmd`.
2. The permission mode the owner chose.
3. Pass or fail for checks 1 to 3 and A to H (including F2), with the terminal lines.
4. For B: every read-back that was wrong, with what the owner actually said.
5. How long each task took from "Sent" to the spoken result.
6. How the spoken summaries sounded: clear, too long, or reading out code or
   file paths awkwardly.
7. Any error text, exactly as printed.

Check that the report contains no keys or tokens before saving.
