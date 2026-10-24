# ka-telemetry

ka-telemetry is a Python command-line tool and library that receives the telemetry Claude Code sends, reads the transcripts Claude Code keeps, and reports what each agent, background run and workflow step used — tokens and time, and cost where Claude Code reported one — on your own machine. It is for people who run Claude Code with subagents, background runs or workflows and want per-agent numbers without a hosted observability service.

How to turn telemetry on, read each report, remove old records and keep the receiver running is in the how-to, [`docs/how-to.md`](docs/how-to.md).

## Built by KensaiApp

ka-telemetry was built end to end by [KensaiApp](https://github.com/eyalgo/kensaiapp), a system of AI roles that carries a software change from first ask to merged code on top of Claude Code. Its roles wrote the requirements, the code, the tests and this README; a person made the decisions. What that cost in Claude runs, tokens and money is in [`docs/build_cost.md`](docs/build_cost.md).

## Install

ka-telemetry supports macOS and Linux, and needs Python 3.11 or later. It depends on nothing outside Python's standard library. Install it from the Python Package Index with pipx, which installs a command-line tool written in Python into an environment of its own:

```sh
pipx install ka-telemetry
```

If you do not have pipx yet, install it first. On macOS, with Homebrew:

```sh
brew install pipx
```

On Linux, install it from your distribution's own package. On Ubuntu 23.04 or later:

```sh
sudo apt update
sudo apt install pipx
```

On another distribution, look for pipx among its own packages.

Then run this once, so that the commands pipx installs, `ka-telemetry` among them, are on your PATH:

```sh
pipx ensurepath
```

The change takes effect in a new terminal. Open one, then run the install command above.

If the Python pipx installs with is older than 3.11, the install does one of two things. It stops with an error and installs nothing, or it installs ka-telemetry with a Python 3.11 or later that pipx chose instead, and says which Python it used. Either way, what pipx prints names ka-telemetry's Python requirement, `>=3.11`, and the rest of its wording differs from one pipx setup to another. If the install stopped, install Python 3.11 or later, then give it to pipx with `--python`, putting the name or the path of the Python you installed in place of `python3.12`:

```sh
pipx install --python python3.12 ka-telemetry
```

## Use the library in your own code

To import ka-telemetry's library in your own Python code, install ka-telemetry with pip inside your own virtual environment, with that environment active:

```sh
python3 -m pip install ka-telemetry
```

This is for your code to import the library. To run the `ka-telemetry` command, install it with pipx, as the Install section above shows.

## Try it

This repository keeps a sample of the telemetry Claude Code sent during a few real sessions, in `tests/stream_parity`. The install above does not bring that sample with it, so get a copy of this repository first and go into it:

```sh
git clone https://github.com/eyalgo/ka-telemetry.git ka-telemetry-repo
cd ka-telemetry-repo
```

Run this example from the top directory of a copy of this repository, the directory that holds the README, which is where those two commands leave you. It prints the per-agent report for one session in that sample, and the block after it is everything the command prints:

```sh
ka-telemetry agents --dir tests/stream_parity --where session.id=6547733b-af89-40a8-a524-2df28af94cae
```

```text
2 agent row(s), 1 unreadable line(s) skipped.
  session=6547733b-af89-40a8-a524-2df28af94cae workflow_run_id=None query_source=repl_main_thread agent=None  requests=1  models=claude-haiku-4-5-20251001  cost_usd=0.051883
    input=10 output=55 cache_read=27180 cache_creation=24440 duration_ms=1550
  session=6547733b-af89-40a8-a524-2df28af94cae workflow_run_id=None query_source=generate_session_title agent=None  requests=2  models=claude-haiku-4-5-20251001  cost_usd=0.001889
    input=1794 output=19 cache_read=0 cache_creation=0 duration_ms=2030
Total cost_usd: 0.053772
Every cost here is an estimate: Claude Code's own, in US dollars as Claude Code reported it, or a total, mean or difference of those estimates. None is a bill.

subagent_completed counts Agent-tool agents only — a Workflow step's own requests (query_source "agent:builtin:workflow-subagent") emit no subagent_completed record, so this count does not cover them.
subagent_completed records were read, but none is in a session the rows show. Run without --where to list them.

Totals check (claude_code.cost.usage vs summed cost_usd):
  session=6547733b-af89-40a8-a524-2df28af94cae  log_cost_usd=0.053772  metric_cost_usd=0.053772
```

Each row is one source of requests in that session: here the main conversation (`repl_main_thread`) and Claude Code's session-title requests (`generate_session_title`), each with its requests, models, cost and token classes. `--where session.id=…` keeps one session's rows; without it, the report shows every session in the directory. The sample holds one line ka-telemetry cannot read, and the report's first line counts it as skipped.

On your own machine, once the receiver has received telemetry, `ka-telemetry agents` with no options reports everything in `~/.ka-telemetry`. The how-to shows how to start the receiver and turn telemetry on.

## What ka-telemetry touches on your machine

Each command touches only what is listed under it, and only when you run it.

**`ka-telemetry receive`**

- Listens on `127.0.0.1`, which only programs on this machine can reach, on the port you give with `--port`.
- Appends each record it accepts to `logs.jsonl` or `metrics.jsonl` in `~/.ka-telemetry`, or in the directory you name with `--dir`. If that directory does not exist, it creates it when it writes its first record.
- With `--publish-endpoint PATH`, writes `http://localhost:PORT` to the file `PATH`, replacing what that file held, with `PORT` the port it is listening on. The directory that holds `PATH` must already exist.
- With `--host` and another address, listens on that address instead, so connections from beyond this machine can reach it. Records that arrive from elsewhere are kept like any other record, and they may be someone else's. The receiver asks for no authentication and writes any JSON it is sent to /v1/logs or /v1/metrics, so with another address anyone who can reach it can add records to these files.

**`ka-telemetry agents` and `ka-telemetry check`**

- Read `logs.jsonl` and `metrics.jsonl` in `~/.ka-telemetry`, or in the directory you name with `--dir`.
- Write no file and run no other program.

**`ka-telemetry runs`**

- Runs `claude agents --json --all`, Claude Code's own command, to list Claude Code's background runs. With `--agents-json FILE`, it reads that list from `FILE` instead and runs no other program.
- Reads the transcripts under `~/.claude/projects`, or under the directory you name with `--projects-root`.
- Reads `~/.ka-telemetry/logs.jsonl`, if it is there, to point at the per-agent report for each session that sent telemetry. No option changes this path.
- Writes no file.

**`ka-telemetry steps`**

- Reads each workflow run's journal, and its agents' transcripts and meta files, under `~/.claude/projects`, or under the directory you name with `--projects-root`.
- Reads `~/.ka-telemetry/logs.jsonl`, if it is there, to point at the per-agent report for each session that sent telemetry. No option changes this path.
- Writes no file and runs no other program.

**`ka-telemetry context`**

- Reads the one transcript file you name.
- Writes no file and runs no other program.

**`ka-telemetry turn-on`**

- Reads the receiver's address from `~/.claude/tools/otel-receiver/endpoint`, or from the file you name with `--endpoint-file`. With `--remove --force`, it does not read it.
- With `--settings-file PATH`, reads the Claude Code settings file `PATH` and writes into it the five keys that make Claude Code send its telemetry to the receiver, with a record of what it wrote. If `PATH` does not exist, it creates it, as long as the directory that holds it exists. With `--remove`, it takes keys out of `PATH` instead.
- Writes the new settings to a temporary file beside `PATH` first, then puts that file in place of `PATH` in one step. If `PATH` is a symbolic link, the file the link points to is the one replaced.
- Without a published address, writes nothing, unless you pass `--remove --force`.
- With `--print`, touches no settings file and prints the keys for one run. With `--print --prefix`, also runs `git rev-parse` in the directory you run it from, to name the git repository you run it in.

## What ka-telemetry will not touch

- Nothing ka-telemetry does deletes, truncates or rewrites a record the receiver wrote. The receiver only appends to its two files, so they grow until you delete them; the how-to shows how.
- `runs`, `steps` and `context` write no file, and change none of Claude Code's files.
- `turn-on`: A value you already set on one of the five keys is never overwritten unless you pass --replace, and --remove takes out only what this command wrote unless you pass --force.
- `turn-on` never turns on Claude Code's logging of prompt text, tool inputs or responses. If you turn that logging on yourself, what Claude Code then sends lands in the receiver's files as received.
- This tool sends no telemetry and makes no network call except the ones the job you asked for requires.
- The receiver keeps what it receives in its two files and sends it nowhere else.

## What the files hold about you

When you are signed in, Claude Code sends your email address with its telemetry, as the `user.email` attribute, along with account, organisation, installation and session identifiers. The receiver removes every `user.email` attribute from each record before it writes the record, so ka-telemetry never writes that email address to disk. `user.email` is the one field the receiver removes, and it keeps everything else as received.

So the account, organisation, installation and session identifiers Claude Code sends do land in the files, and sharing the files — in a bug report, for example — shares those identifiers. If you turn on Claude Code's logging of prompt text, tool inputs or responses, that text lands in the files too, with any email address written inside it.

## Other providers

ka-telemetry is built to read Claude Code. Other providers are not fully supported:

- Codex and Gemini: ka-telemetry has no reader for either, so it reports nothing they use.
- Claude Code on a non-Anthropic endpoint, such as a local model: the cost ka-telemetry shows for it is Claude Code's guess.

## Check a release

Every release of ka-telemetry has to meet six bars, numbered 1, 2, 3, 5, 7 and 8 among its success metrics. The release check measures all six of them. It measures bars 1, 2 and 3 on the reference set, the recorded telemetry and transcripts this repository keeps in `tests/parity` and `tests/stream_parity` with the figures expected from them. It measures bar 5 by building ka-telemetry from your copy and then running this README's own pipx install command, the first command in the Install section above, as written, on a Python that refuses pip installs as externally managed. What it built is the only package that command can install. It measures bar 7 by running every `ka-telemetry` command, refusing any attempt a command makes to reach beyond this machine, and by checking that the receiver, started with no `--host`, listens only on this machine. It does not watch the programs a command starts, such as the Git that `turn-on` runs. It measures bar 8 on the examples in this README and the how-to. Run it from the top directory of a copy of this repository, the directory that holds the README:

```sh
python3 tools/release_check.py
```

It needs Python 3.11 or later with pip, pytest installed for that Python, and Git. It measures the files in your copy as they are, including changes you have not committed, and changes none of them: it works in a temporary directory that it removes before it exits. The settings file `turn-on` writes during the check and the files the receiver writes are in that directory too, and the receiver is stopped before the check ends. For bar 5 it uses the pipx on your PATH, or, when there is none, a pipx of its own that it brings into that directory. What pipx writes during the check is in that directory as well.

The check fetches packages over the network with pip in these three steps and in no other, and none of them is part of the install it measures:

- Each build of ka-telemetry, one for the install and one for the examples, fetches its build backend.
- When there is no pipx on your PATH, bringing in the check's own pipx fetches pipx and the packages pipx depends on.
- Before the install it measures, readying the libraries pipx shares among the environments it makes, pip among them, fetches those libraries. This step runs whether the pipx is yours or the check's own.

The install it measures fetches no package: it installs only what it built.

It prints one line for each bar it checks: the bar's number, what the bar asks, and then `holds` or `fails`. A bar it could not measure ends `fails` too. For each line that ends `fails`, it writes lines to standard error that begin with the same number and say what did not hold, or what it could not read or run. It exits with status 0 when every line ends `holds`, and with status 1 when any line ends `fails`.

## Support

Support is best effort, with no response-time promise. ka-telemetry is maintained while kensaiapp runs on it; kensaiapp is the publisher's own system for building software with Claude Code agents, the counting ka-telemetry does was first written there, and kensaiapp now reads its own telemetry through ka-telemetry. If that stops, the repository is archived visibly rather than left with an open issue tracker. This support posture is a personal promise from the person who publishes ka-telemetry, and no company stands behind it.

To report a security problem privately, open the repository's Security tab on GitHub and choose **Report a vulnerability** (https://github.com/eyalgo/ka-telemetry/security/advisories/new); [`SECURITY.md`](SECURITY.md) says the same. Please do not open a public issue for it. Whether outside pull requests are taken is in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Versions and the public API

ka-telemetry follows semantic versioning against its public API, which is: the command names and their options, the exit codes, both output forms (text and `--json`), the layout of the files the receiver writes and the outer fields of each record in them (`signal`, `received_at` and `body`), which settings entries `turn-on` writes, and the library's readers and row types.

Two things sit outside the public API, so a change Claude Code makes to either is not a breaking change in ka-telemetry: the content of each record, which is what Claude Code sent less the dropped email address, and the names of the settings entries, which are Claude Code's.

ka-telemetry is at 0.x, and until 1.0.0 anything may change. kensaiapp now runs on ka-telemetry, so 1.0.0 is declared once someone with no tie to the project has got started from this README.

## Status

What each release does is in [`release_notes/`](release_notes/), one file per release.

The totals check names every session that has no cost metric to check against, says it received none for it, and gives the causes it cannot tell apart without claiming one, which came in with commit `c83c77761`.

ka-telemetry does not yet state in every report what it read and what it could not read and why.
