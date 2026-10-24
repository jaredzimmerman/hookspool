# Private bobbins and hooks

A deployment never edits a built-in bobbin. When a built-in bobbin does not do what you need, there are three ways to
change that, from the lightest to the heaviest.

## 1. Settings

Almost everything a bobbin lets you tune is a setting: a time zone, working hours, domains to skip, a state directory.
Set them in the instance's `settings` table. `hookspool bobbin show <bobbin>` lists them with their types and defaults.

## 2. A hooks module

A bobbin that supports hooks has a `hooks` setting, an absolute path to a Python file of yours. The bobbin's README names
the functions it calls and their signatures. For example:

| bobbin | function | purpose |
|---|---|---|
| `gmail` | `hooks.classify(message, account, ctx) -> dict or None` | hand a message off privately before the bobbin's own rules run |
| `gcal` | `hooks.travel(event, day_events, ctx) -> dict or None` | decide about travel blocks around an event |

The function receives the processor context (`ctx.settings`, `ctx.http`, `ctx.load_state` and so on). A hook that raises
retries the batch and changes nothing. The rules that keep a built-in bobbin small do not apply to hooks, because a hooks
module is your code, not the bobbin's.

```toml
[bobbins.gmail]
settings = { owner = "owner@example.com", hooks = "/etc/hookspool/hooks/gmail_hooks.py" }
```

## 3. A private bobbin

When the service is not in the repository at all, write a bobbin ([Writing a bobbin](writing-a-bobbin.md)) in a
directory of your own and list the parent directory in the config:

```toml
[spool]
bobbin_path = ["/etc/hookspool/bobbins"]
```

Each subdirectory of a `bobbin_path` entry is a bobbin, searched after the built-in ones. It may not share a name with a
built-in bobbin. It is loaded by file path under a private module name and is never added to `sys.path`, so it cannot
shadow another module. `hookspool bobbin list` shows where each one came from, and `hookspool bobbin check <dir>` runs the
same checks on it as on a bobbin in the repository.

## What the loader insists on

Code loaded by path, whether a `bobbin_path` directory or a hooks module, must be an absolute path to a regular file or
directory that is not writable by its group or by others, and is owned by root or by the user hookspool runs as.
`hookspool check` verifies this the same way it verifies secret files.

Manifests and bobbin directories are read again on every config load. Editing a `bobbin.toml` or adding a directory does not
change `config.toml`, so it takes effect at the next `hookspool reload` or service restart. Processor code and hooks modules
are loaded fresh by each processor run, so a change applies to the next batch.
