# Release notes — ka-telemetry

## 0.1.0

The first release. What it does:

- The stream reports of ka-telemetry name the stream files they could not read and the ones they did not find.
- When the receiver starts, ka-telemetry says where it is listening and where it is writing.
- The runs and steps reports of ka-telemetry say they carry no cost, and point at the per-agent report for each session that sent telemetry.
- The runs and steps reports of ka-telemetry count the lines they could not read in the transcripts.
- The steps report of ka-telemetry counts the lines it could not read in a workflow run's journal.
- The steps report of ka-telemetry counts the agent meta files it could not read, and says their agents' types show as (none).
- The runs report of ka-telemetry names each run whose transcript it found and could not open.
- The steps report of ka-telemetry says when it found a workflow run's journal and could not open it, and reads none of that run's agent files.
- The context command of ka-telemetry says the context size is unknown when it found the transcript and could not open it, and counts the unreadable lines it skipped.
- The runs report of ka-telemetry says why it could not read each run it names as unreadable: the transcript is gone, it could not be opened, or a directory it may be in could not be looked in.
- The runs and steps reports of ka-telemetry name each directory they could not look in, and say to run the command again once it can be looked in.
- The steps report of ka-telemetry names every directory it could not look in on the way to a workflow run — the projects root, a project, a session, its `subagents` or `workflows` directory, or the run's own directory —.
- When `ka-telemetry steps --wf` finds no such workflow run, it adds a line for each directory it could not look in and says to run the command again once it can be looked in.
- The steps report of ka-telemetry names each workflow run whose directory it could not list, says it has no count of that run's agent files with no journal entry at all, and says to run the command again once that directory can be listed.
- The steps report of ka-telemetry gives each workflow step its span in seconds, in the text and in the JSON.
- The steps report of ka-telemetry measures each row's time from the first to the last model response it counted for that row, and gives no time for a row with no response it could time.
- The runs and steps reports of ka-telemetry each say what their time covers, so a row with one response that reads 0 seconds is not taken for one that took no time.
- ka-telemetry is licensed under Apache-2.0.
- The README of ka-telemetry says what it does in one sentence, gives one install path and one example to copy with its expected output, and says what it will and will not touch.
- The how-to of ka-telemetry shows how to turn telemetry on for Claude Code for every run and for one run, walks the four main reports, and shows how to remove old records.
- The how-to of ka-telemetry shows how to keep the receiver running.
- The how-to of ka-telemetry shows how to have ka-telemetry start at login, on macOS and on Linux.
- The README of ka-telemetry says that the content of each record and the names of the settings entries sit outside its public API.
- The README of ka-telemetry says that until 1.0.0 anything may change.
- The README of ka-telemetry says that ka-telemetry never writes your email address to disk, names the identifiers that do land in the files, and warns that sharing those files shares them.
- The README of ka-telemetry says, beside the option that lets the receiver accept connections from beyond your machine, that records arriving from elsewhere may be someone else's.
- The runs and steps reports of ka-telemetry count a response that appears in more than one transcript once, under one row only, and say in their coverage line how many such responses they found.
- The README of ka-telemetry gives `pipx install ka-telemetry` as the one install command, and says how to get pipx on macOS and from a Linux distribution's own package and how to put pipx's commands on your PATH.
- The README of ka-telemetry tells a developer who imports the library to install it with pip inside their own virtual environment, away from the install command.
- The README of ka-telemetry says what the install prints when the Python it uses is older than 3.11, and to install Python 3.11 or later and give it to pipx.
- The release check of ka-telemetry measures the install bar on what the README's own install command installs, run on a Python that refuses pip installs as externally managed.

What it does not do yet:

- ka-telemetry does not yet state in every report what it read and what it could not read and why, or name every session that has no cost metric to check against and say it received no cost metric for it.
