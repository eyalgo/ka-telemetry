# How to use ka-telemetry

This how-to is for someone who has installed ka-telemetry as the [README](../README.md) shows and wants to record their own Claude Code work. It shows how to turn telemetry on for every Claude Code run or for one run, read the four main reports, remove old records, and keep the receiver running, including starting it when you log in.

## Turn telemetry on for every Claude Code run

Take these three steps in this order. The receiver has to be running, and has to have published its address, before `turn-on` can write anything.

1. Make the directory the receiver publishes its address in:

```sh
mkdir -p ~/.claude/tools/otel-receiver
```

2. Start the receiver. It serves until it is stopped, so start it in the background, with what it prints going to a log file, and you get your terminal back at once:

```sh
nohup ka-telemetry receive --port 4318 --publish-endpoint ~/.claude/tools/otel-receiver/endpoint >> ~/ka-telemetry-receiver.log 2>&1 &
```

Then read its log:

```sh
cat ~/ka-telemetry-receiver.log
```

Once the log holds a line that starts `The receiver is listening on 127.0.0.1, port 4318`, the receiver has written its address, `http://localhost:4318`, to `~/.claude/tools/otel-receiver/endpoint`, where `turn-on` reads it. Port 4318 is the default port for OpenTelemetry over HTTP. If another program already listens on it, the receiver stops, and the log ends in `Address already in use`. To see which program holds the port, run `lsof -nP -iTCP:4318 -sTCP:LISTEN`. Stop that program, or start the receiver again with another port, such as 4319, and use that port wherever this how-to says 4318.

A receiver started this way runs until you stop it, log out or restart the machine. To have it start again by itself, see [Keep the receiver running](#keep-the-receiver-running).

3. Write the keys into Claude Code's user settings file:

```sh
ka-telemetry turn-on --settings-file ~/.claude/settings.json
```

It prints `wrote CLAUDE_CODE_ENABLE_TELEMETRY, OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER, OTEL_EXPORTER_OTLP_PROTOCOL, OTEL_EXPORTER_OTLP_ENDPOINT`. If the settings file does not exist, it creates it. If you had already set one of those keys yourself, it leaves your value, names the key on a line that starts `left`, and says how to replace it. If it prints `no published telemetry endpoint` and `nothing written`, the receiver has not published its address: check that the receiver's log shows the line from step 2, then run this step again.

Claude Code sessions you start from now on send their telemetry to the receiver. To see what has arrived, run `ka-telemetry agents`. If you change the receiver's port later, start the receiver on the new port and run step 3 again: it replaces the address it wrote.

To stop the receiver you started in step 2:

```sh
pkill -f "ka-telemetry receive"
```

While it is stopped, nothing Claude Code sends is recorded; [Keep the receiver running](#keep-the-receiver-running) shows how to have it start when you log in.

## Turn telemetry on for one Claude Code run

With the receiver running, as steps 1 and 2 above show, start that one run like this:

```sh
claude --settings "$(ka-telemetry turn-on --print)"
```

`turn-on --print` reads the address the receiver published and prints the five keys as the settings JSON that `claude --settings` accepts, so that run gets the five keys and no settings file is written or changed. Add your other `claude` arguments after it as usual. The line it prints that starts `Telemetry:` goes to the terminal and is not part of the settings.

If it finds no published address, it prints a line that starts `(no published telemetry endpoint` and prints no settings, so that run gets none of the five keys from it. Stop that run, start the receiver as step 2 shows, and start the run again.

## Read the reports

On your own data, `agents` and `check` read the whole of the receiver's two files every time they run, and print nothing until they have read them. The files only grow, so a report reads more each time. On one machine, one run of `tools/measure_stream_cost.py` measured both reports on stream data it generated, a `logs.jsonl` and a `metrics.jsonl` like the receiver's two files, at three sizes of the two files together, and printed each report's peak memory in MB, where 1 MB is 1,000,000 bytes, and its time from start to finish in seconds (s). At 100 MB, `agents` peaked at 474.5 MB and took 1.8 s, and `check` peaked at 475.0 MB and took 1.8 s. At 300 MB, `agents` peaked at 1309.9 MB and took 5.7 s, and `check` peaked at 1361.7 MB and took 5.7 s. At 1 GB, `agents` peaked at 3415.0 MB and took 20.4 s, and `check` peaked at 3366.4 MB and took 21.6 s. These figures are the cost of reading the whole of both files, as the reports do, and are not a target or a limit; on another machine they differ. To measure your own machine, run `python3 tools/measure_stream_cost.py` from the top directory of a copy of this repository. It needs macOS or Linux and Python 3.11 or later, and no installed ka-telemetry; it writes its stream data, at least 1 GB at the largest size, in a temporary directory outside the copy, and removes it when it finishes, fails or is stopped with Ctrl-C. `--where` narrows what a report prints, not what it reads. To keep reports quick, remove old records as [Remove old records](#remove-old-records) shows.

Each of the four main reports is shown below on sample data kept in this repository's `tests` directory, with everything it prints. Run these examples from the top directory of a copy of this repository, the directory that holds the README. After each one comes where the report reads by default, so you can run it on your own data.

### The per-agent report

```sh
ka-telemetry agents --dir tests/stream_parity
```

```text
16 agent row(s), 1 unreadable line(s) skipped.
  session=ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593 workflow_run_id=None query_source=repl_main_thread agent=None  requests=15  models=claude-opus-5-5  cost_usd=0.950151
    input=30 output=9311 cache_read=826617 cache_creation=74811 duration_ms=104415
  session=ddb179fa-fcdf-40fc-8fd0-8ca1bbc0f9d6 workflow_run_id=None query_source=sdk agent=None  requests=3  models=claude-haiku-4-5-20251001  cost_usd=0.064833
    input=30 output=670 cache_read=90974 cache_creation=26178 duration_ms=9013
  session=83a3a897-661c-46fa-a2be-a94d37f013e1 workflow_run_id=None query_source=sdk agent=None  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.058177
    input=18 output=1082 cache_read=38952 cache_creation=24427 duration_ms=16089
  session=6547733b-af89-40a8-a524-2df28af94cae workflow_run_id=None query_source=repl_main_thread agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.051883
    input=10 output=55 cache_read=27180 cache_creation=24440 duration_ms=1550
  session=7931ae7c-d999-4286-b091-261680002258 workflow_run_id=None query_source=sdk agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.050333
    input=10 output=42 cache_read=13607 cache_creation=24376 duration_ms=1222
  session=4d134142-c03d-4e8c-a5e7-c51658727f35 workflow_run_id=None query_source=agent:builtin:Explore agent=Explore  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.038118
    input=18 output=1166 cache_read=11073 cache_creation=24930 duration_ms=17133
  session=e0fc17f8-99f6-4206-a895-fcde68af74da workflow_run_id=None query_source=agent:builtin:Explore agent=Explore  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.033823
    input=18 output=576 cache_read=10096 cache_creation=23932 duration_ms=8119
  session=4d134142-c03d-4e8c-a5e7-c51658727f35 workflow_run_id=None query_source=sdk agent=None  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.022715
    input=18 output=430 cache_read=42987 cache_creation=8124 duration_ms=4592
  session=ddb179fa-fcdf-40fc-8fd0-8ca1bbc0f9d6 workflow_run_id=None query_source=agent:builtin:Explore agent=Explore  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.016181
    input=10 output=77 cache_read=0 cache_creation=12629 duration_ms=1432
  session=e0fc17f8-99f6-4206-a895-fcde68af74da workflow_run_id=None query_source=sdk agent=None  requests=3  models=claude-haiku-4-5-20251001  cost_usd=0.012605
    input=28 output=492 cache_read=76746 cache_creation=1221 duration_ms=6255
  session=56258bcf-fe22-4a20-aace-46d5dd412ec7 workflow_run_id=None query_source=sdk agent=None  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.009066
    input=18 output=325 cache_read=50690 cache_creation=1177 duration_ms=4127
  session=6547733b-af89-40a8-a524-2df28af94cae workflow_run_id=None query_source=generate_session_title agent=None  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.001889
    input=1794 output=19 cache_read=0 cache_creation=0 duration_ms=2030
  session=e0fc17f8-99f6-4206-a895-fcde68af74da workflow_run_id=None query_source=generate_session_title agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.001056
    input=976 output=16 cache_read=0 cache_creation=0 duration_ms=895
  session=4d134142-c03d-4e8c-a5e7-c51658727f35 workflow_run_id=None query_source=generate_session_title agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.001051
    input=976 output=15 cache_read=0 cache_creation=0 duration_ms=993
  session=83a3a897-661c-46fa-a2be-a94d37f013e1 workflow_run_id=None query_source=generate_session_title agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.001036
    input=966 output=14 cache_read=0 cache_creation=0 duration_ms=807
  session=56258bcf-fe22-4a20-aace-46d5dd412ec7 workflow_run_id=None query_source=generate_session_title agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.001036
    input=966 output=14 cache_read=0 cache_creation=0 duration_ms=725
Total cost_usd: 1.313953
Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, or a total, mean or difference of those estimates. None is a bill.

subagent_completed counts Agent-tool agents only — a Workflow step's own requests (query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so this count does not cover them.
  session=4d134142-c03d-4e8c-a5e7-c51658727f35 agent_type=Explore count=1 duration_ms=17333 total_tool_uses=1
  session=ddb179fa-fcdf-40fc-8fd0-8ca1bbc0f9d6 agent_type=Explore count=1 duration_ms=1567 total_tool_uses=0
  session=e0fc17f8-99f6-4206-a895-fcde68af74da agent_type=Explore count=1 duration_ms=8345 total_tool_uses=1

Totals check (claude_code.cost.usage vs summed cost_usd):
  session=4d134142-c03d-4e8c-a5e7-c51658727f35  log_cost_usd=0.061883  metric_cost_usd=0.061883
  session=56258bcf-fe22-4a20-aace-46d5dd412ec7  log_cost_usd=0.010102  metric_cost_usd=0.010102
  session=6547733b-af89-40a8-a524-2df28af94cae  log_cost_usd=0.053772  metric_cost_usd=0.053772
  session=7931ae7c-d999-4286-b091-261680002258  log_cost_usd=0.050333  no metric export — possible causes, which ka-telemetry cannot tell apart: the session may have ended before Claude Code exported a cost metric for it; the session may still be running; the receiver (ka-telemetry receive) may not have been running when Claude Code sent the cost metric
  session=83a3a897-661c-46fa-a2be-a94d37f013e1  log_cost_usd=0.059213  metric_cost_usd=0.059213
  session=ddb179fa-fcdf-40fc-8fd0-8ca1bbc0f9d6  log_cost_usd=0.081015  metric_cost_usd=0.081015
  session=e0fc17f8-99f6-4206-a895-fcde68af74da  log_cost_usd=0.047483  metric_cost_usd=0.047483
  session=ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593  log_cost_usd=0.950151  metric_cost_usd=0.727485  MISMATCH >1% — possible causes, which ka-telemetry cannot tell apart: the session may still be running, or may have ended before Claude Code exported all of it, so one of its two totals may not have caught up with the other; the receiver (ka-telemetry receive) may not have been running for part of the session, so some of what Claude Code sent may never have arrived; Claude Code's request costs and its cost metric may really disagree
```

The per-agent report has one row for each session, workflow run, query source and agent, highest cost first, with its requests, its models, its cost and four token classes, which are never added together. Under the rows come the total, the Agent-tool agents Claude Code reported as completed, and the totals check for the sessions the rows show. Every cost in it is the one Claude Code reported.

A figure followed by `(invalid: n)` leaves out n values that the row's requests carried for it and that were not a number, such as `true` or `"abc"`, so it is the sum of the values that were numbers rather than of all of them; a request that carried no value for that figure at all is not counted. The `Total cost_usd` line counts the costs left out across all the rows in the same way. With `--json`, each row that left a value out carries its counts in an `invalid_values` member, named by the keys of the figures they touch, and the total's count sits in an `invalid_values` member after `total_cost_usd`.

The same mark appears in the rows under the line that begins `subagent_completed counts Agent-tool agents only`: there, a `duration_ms` or `total_tool_uses` followed by `(invalid: n)` leaves out n values that the row's `subagent_completed` records carried for that figure and that were not a number, so it too is the sum of the values that were numbers. A record that carried no value for that figure is not counted, and every one of the row's records is still counted in its `count=`, whether or not it left a value out. With `--json`, each of those rows that left a value out carries its counts in an `invalid_values` member after `total_tool_uses`, named by the keys of the figures they touch.

The same mark also appears in the block that `--compare ATTR A B` adds after the totals check, which sets two sides next to each other: the requests whose attribute `ATTR` is `A`, and those whose `ATTR` is `B`. There, a figure of a side followed by `(invalid: n)` leaves out n values that the side's requests carried for that figure and that were not a number, so the side's total and its mean per run for that figure are taken from the values that were numbers. Beside `mean_cost_per_run` and `total_cost_usd`, n counts the side's costs that were left out. A request that carried no value for that figure is not counted, and a request that left a value out still counts toward its run in `runs=`. `--where` narrows these counts as it narrows the figures beside them. The two lines under the sides, which subtract and divide their `mean_cost_per_run`, carry no mark of their own; they are worked out from those two figures, so a cost left out of either side is left out of them too. With `--json`, a side in `compare` that left a value out carries its counts in an `invalid_values` member after `mean_per_run`, named by the keys of the figures they touch.

By default it reads `logs.jsonl` and `metrics.jsonl` in `~/.ka-telemetry`, where the receiver writes them; `--dir` names another directory. On your own data, run `ka-telemetry agents`.

### The totals check

```sh
ka-telemetry check --dir tests/stream_parity
```

```text
Unreadable lines in logs.jsonl, skipped and left out of the totals check: 1.
Totals check (claude_code.cost.usage vs summed cost_usd):
  session=4d134142-c03d-4e8c-a5e7-c51658727f35  log_cost_usd=0.061883  metric_cost_usd=0.061883
  session=56258bcf-fe22-4a20-aace-46d5dd412ec7  log_cost_usd=0.010102  metric_cost_usd=0.010102
  session=6547733b-af89-40a8-a524-2df28af94cae  log_cost_usd=0.053772  metric_cost_usd=0.053772
  session=7931ae7c-d999-4286-b091-261680002258  log_cost_usd=0.050333  no metric export — possible causes, which ka-telemetry cannot tell apart: the session may have ended before Claude Code exported a cost metric for it; the session may still be running; the receiver (ka-telemetry receive) may not have been running when Claude Code sent the cost metric
  session=83a3a897-661c-46fa-a2be-a94d37f013e1  log_cost_usd=0.059213  metric_cost_usd=0.059213
  session=ddb179fa-fcdf-40fc-8fd0-8ca1bbc0f9d6  log_cost_usd=0.081015  metric_cost_usd=0.081015
  session=e0fc17f8-99f6-4206-a895-fcde68af74da  log_cost_usd=0.047483  metric_cost_usd=0.047483
  session=ee5d2a51-f34e-42e0-b38b-fa8dd8c5b593  log_cost_usd=0.950151  metric_cost_usd=0.727485  MISMATCH >1% — possible causes, which ka-telemetry cannot tell apart: the session may still be running, or may have ended before Claude Code exported all of it, so one of its two totals may not have caught up with the other; the receiver (ka-telemetry receive) may not have been running for part of the session, so some of what Claude Code sent may never have arrived; Claude Code's request costs and its cost metric may really disagree
Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, or a total, mean or difference of those estimates. None is a bill.
```

The totals check sets two figures side by side for each session: the cost Claude Code reported with each request, added up, and Claude Code's own cost metric for the session. A session whose two figures differ by more than 1% is marked `MISMATCH >1%`, and a session with no cost metric is marked `no metric export`. Each mark lists the causes ka-telemetry cannot tell apart, and neither is an error: the command exits 0.

A figure followed by `(invalid: n)` leaves out n values that were not a number, such as `true` or `"abc"`, so it is the sum of the values that were numbers rather than of all of them. Beside `log_cost_usd`, n counts the session's requests that carried a cost that was not a number; beside `metric_cost_usd`, it counts the points of Claude Code's cost metric for the session that carried a value that was not a number. A request that carried no cost, and a point that carried no value, are not counted. Values left out are not among the causes a `MISMATCH >1%` mark lists, but they can be why the two figures differ. With `--json`, a session that left a value out carries its counts in an `invalid_values` member, named by the keys of the figures they touch.

By default it reads the same directory as the per-agent report, `~/.ka-telemetry`; `--dir` names another one. On your own data, run `ka-telemetry check`.

### The per-run report

```sh
ka-telemetry runs --agents-json tests/parity/agents.json --projects-root tests/parity/projects
```

```text
Population: background runs only (rows with kind != "interactive"), and — with --session — every run named by its session id.
Span: wall-clock time from the first to the last model response counted for the run or step, not from when it started; one response reads 0s, and n/a means no counted response carried a readable time.
Coverage: 3 read / 0 unreadable / 3 total (3 listed, 0 named)
Dropped (no sessionId): 1

a839285c-263f-40a6-8801-71f49d00cc68  listed-and-named  cwd=/work/example  state=completed
  started=2026-10-04T11:12:51.000Z  span=15.544s
  claude-sonnet-5-5: input=10 output=1557 thinking=34 cache_read=131826 cache_write_1h=27067 cache_write_5m=0

15b8016a-f529-44fe-b6ea-1013fea8a4a8  listed-only  cwd=/work/example  state=completed
  started=2026-10-05T09:00:00.000Z  span=0s
  claude-haiku-4-5-20251001: input=10 output=98 thinking=84 cache_read=23912 cache_write_1h=0 cache_write_5m=0

7ef055ef-5823-4b0d-845c-efc5b4fdab28  listed-no-usage  cwd=/work/example  state=completed
  started=2026-10-06T09:00:00.000Z  span=n/a
  no usage records
This report carries no cost, because ka-telemetry never computes a cost from tokens.
```

The per-run report shows the tokens each background run used, read from its transcript, for each model the run used, with each token class shown apart. It shows no cost, because ka-telemetry never computes a cost from tokens. The `Coverage` line says how many runs it read and how many it could not. The sample list holds one entry with no session id, which the report counts as dropped.

By default it lists Claude Code's background runs by running `claude agents --json --all`, and reads their transcripts under `~/.claude/projects`; `--agents-json` reads a saved copy of that list instead, and `--projects-root` names another transcript directory. To add a run that `claude agents` does not list, such as a `claude -p` run, name its session id with `--session`. It also reads `~/.ka-telemetry/logs.jsonl`, if it is there, to point at the per-agent report for each session that sent telemetry. On your own data, run `ka-telemetry runs`.

### The per-step report

```sh
ka-telemetry steps --projects-root tests/parity/projects
```

```text
Fallback: an agent whose journal result carries no step (or none this reader could attribute to a producer or an auditor) is grouped by its agentType instead, marked fallback so a reader can tell a step from a name.
Span: wall-clock time from the first to the last model response counted for the run or step, not from when it started; one response reads 0s, and n/a means no counted response carried a readable time.

wf_ec5e24de-808  session=174d0111-bfa9-4539-92d9-5560e0ab4c36  slug=-home-user-work-demo
  Coverage: 9 with a step / 1 without / 0 started with no result / 0 agent files with no journal entry at all
  development-plan (produce)  types=tech-lead  agents=a53ef7efd8c468bad
    span=113.727s
    claude-opus-5: input=40 output=415 thinking=0 cache_read=1459608 cache_write_1h=0 cache_write_5m=103692
  pm [fallback]  types=pm  agents=a85c5919a45174165
    span=93.63s
    claude-opus-5: input=26 output=71 cache_read=741398 cache_write_1h=0 cache_write_5m=78011
  development-code-integrity (produce)  types=senior-engineer  agents=ab58aa559157db106
    span=366.182s
    claude-opus-5: input=32 output=218 thinking=0 cache_read=1012546 cache_write_1h=0 cache_write_5m=102593
  development-specs (audit)  types=kensaiapp-auditor  agents=ac342e357894438af
    span=215.143s
    claude-opus-5: input=50 output=856 thinking=0 cache_read=1434647 cache_write_1h=0 cache_write_5m=74477
  development-design (produce)  types=architect  agents=ad648eb1f468fdd44
    span=118.955s
    claude-opus-5: input=30 output=46 cache_read=1065692 cache_write_1h=0 cache_write_5m=116529
  development-implementation (audit)  types=kensaiapp-auditor  agents=adea8139719961545
    span=177.325s
    claude-opus-5: input=42 output=580 thinking=0 cache_read=1170088 cache_write_1h=0 cache_write_5m=78427
  development-specs (produce)  types=product-owner  agents=af246bebab758f705
    span=144.245s
    claude-opus-5: input=30 output=62 cache_read=879180 cache_write_1h=0 cache_write_5m=94060
  development-plan (audit)  types=kensaiapp-auditor  agents=af81dac1517cc2aa3
    span=286.435s
    claude-opus-5: input=50 output=920 thinking=0 cache_read=1800479 cache_write_1h=0 cache_write_5m=115476
  development-implementation (produce)  types=developer  agents=afc6d5e9e874cf67f
    span=86.566s
    claude-sonnet-5: input=44 output=505 thinking=0 cache_read=2094533 cache_write_1h=0 cache_write_5m=143692
  development-design (audit)  types=kensaiapp-auditor  agents=aff5cbaa50495ff32
    span=270.837s
    claude-opus-5: input=44 output=285 thinking=0 cache_read=1347549 cache_write_1h=0 cache_write_5m=97922
This report carries no cost, because ka-telemetry never computes a cost from tokens.
```

The per-step report shows, for each workflow run, the tokens each step used, read from the run's journal and its agents' transcripts. An agent whose journal entry names no step is grouped under its agent type and marked `[fallback]`. It shows no cost.

By default it reads under `~/.claude/projects`; `--projects-root` names another directory, and `--wf` with a workflow run's id reports that run alone. It also reads `~/.ka-telemetry/logs.jsonl`, if it is there, to point at the per-agent report for each session that sent telemetry. On your own data, run `ka-telemetry steps`.

## Remove old records

The receiver keeps its records in two files in `~/.ka-telemetry`, or in the directory you gave it with `--dir`: `logs.jsonl` holds the log records Claude Code sends, one of them for each request, and `metrics.jsonl` holds its metric records. To remove old records, delete a whole file, with the receiver still running.

To delete the log records:

```sh
rm ~/.ka-telemetry/logs.jsonl
```

To delete the metric records:

```sh
rm ~/.ka-telemetry/metrics.jsonl
```

To delete both:

```sh
rm ~/.ka-telemetry/logs.jsonl ~/.ka-telemetry/metrics.jsonl
```

Deleting logs.jsonl or metrics.jsonl removes the records that file holds; doing so never requires stopping the receiver, and never loses or corrupts a record being written at the time. ka-telemetry keeps no other copy, so the deleted records are gone. The next record the receiver accepts starts a new file at the same path.

That covers deleting a whole file and nothing else, so delete the file rather than emptying it, editing it or moving it.

## Keep the receiver running

The receiver records only while it runs. What Claude Code sent while the receiver was not running never reached ka-telemetry, and ka-telemetry cannot recover it, so every report covers only what the receiver received.

To have the receiver running whenever you are logged in, have your system start it at login. ka-telemetry does not install itself to start at login. The steps below are yours to take: each sets up your system's own service manager to start the receiver when you log in, with the same port and address file as the steps above, writing to `~/.ka-telemetry`. First stop any receiver you started by hand, as [Turn telemetry on for every Claude Code run](#turn-telemetry-on-for-every-claude-code-run) shows, so that two receivers do not try to use the same port.

### Start the receiver at login on macOS

1. Make the directories the receiver and its login file need:

```sh
mkdir -p ~/.claude/tools/otel-receiver ~/Library/LaunchAgents
```

2. Print the full path of the installed command, then of your home directory:

```sh
command -v ka-telemetry
echo "$HOME"
```

3. Create the file `~/Library/LaunchAgents/ka-telemetry.receiver.plist` with the content below. Replace `/path/to/ka-telemetry` with the first line step 2 printed, and each `/Users/you` with the second. The file needs full paths, because launchd does not expand `~` or `$HOME` in it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ka-telemetry.receiver</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/ka-telemetry</string>
    <string>receive</string>
    <string>--port</string>
    <string>4318</string>
    <string>--publish-endpoint</string>
    <string>/Users/you/.claude/tools/otel-receiver/endpoint</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/you/Library/Logs/ka-telemetry-receiver.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/Library/Logs/ka-telemetry-receiver.log</string>
</dict>
</plist>
```

4. Load it. launchd starts the receiver now, and again each time you log in:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ka-telemetry.receiver.plist
```

5. Check that it started: `~/Library/Logs/ka-telemetry-receiver.log` holds a line that starts `The receiver is listening on 127.0.0.1, port 4318`. If the log shows `Address already in use` instead, another program holds port 4318. Change 4318 in the file, run `launchctl bootout gui/$(id -u)/ka-telemetry.receiver`, load the file again as step 4 shows, and run step 3 of [Turn telemetry on for every Claude Code run](#turn-telemetry-on-for-every-claude-code-run) again. If you have not yet written the keys into Claude Code's settings, take that step 3 now.

To stop the receiver starting at login, unload the file and delete it:

```sh
launchctl bootout gui/$(id -u)/ka-telemetry.receiver
rm ~/Library/LaunchAgents/ka-telemetry.receiver.plist
```

### Start the receiver at login on Linux

These steps need a Linux system that runs systemd with user services, where `systemctl --user` works.

1. Make the directories the receiver and its login file need:

```sh
mkdir -p ~/.claude/tools/otel-receiver ~/.config/systemd/user
```

2. Print the full path of the installed command:

```sh
command -v ka-telemetry
```

3. Create the file `~/.config/systemd/user/ka-telemetry-receiver.service` with the content below. Replace `/path/to/ka-telemetry` with the path step 2 printed. Leave `%h` as it is: systemd replaces it with your home directory.

```ini
[Unit]
Description=ka-telemetry receiver

[Service]
ExecStart=/path/to/ka-telemetry receive --port 4318 --publish-endpoint %h/.claude/tools/otel-receiver/endpoint
Restart=on-failure

[Install]
WantedBy=default.target
```

4. Enable it. systemd starts the receiver now, and again each time you log in:

```sh
systemctl --user daemon-reload
systemctl --user enable --now ka-telemetry-receiver.service
```

5. Check that it started: `systemctl --user status ka-telemetry-receiver.service` shows `active (running)` and a line that starts `The receiver is listening on 127.0.0.1, port 4318`. If it shows `Address already in use` instead, another program holds port 4318. Change 4318 in the file, run `systemctl --user daemon-reload` and then `systemctl --user restart ka-telemetry-receiver.service`, and run step 3 of [Turn telemetry on for every Claude Code run](#turn-telemetry-on-for-every-claude-code-run) again. If you have not yet written the keys into Claude Code's settings, take that step 3 now.

To stop the receiver starting at login, disable it and delete the file:

```sh
systemctl --user disable --now ka-telemetry-receiver.service
rm ~/.config/systemd/user/ka-telemetry-receiver.service
```
