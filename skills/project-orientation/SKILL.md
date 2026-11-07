---
name: project-orientation
title: Project orientation
version: 0.1.1
type: extension
plugin_api: '2.0'
entry: plugin.py
os: any
permissions: [route, fs, subprocess]
description: Read-only project folder and Git checkout identity for the built-in interface.
when_to_use: The project information disclosure asks where a project's configured folder lives.
---

# Project orientation

GET `info?project_id=<registered-id>` returns a dated observation of the active project's configured folder and its Git checkout, if any. It accepts no arbitrary path and performs no Git writes, network operations, initialization, hooks or repository content reads. No folder and an ordinary non-Git folder are normal states; unreadable or failed inspection is unavailable, not absence.

This in-process extension deliberately depends on the installed host's internal `ouroboros.projects_registry.get_reserved_project(..., strict=True)` reader, using `PluginAPI.get_runtime_info().data_dir`. That import is not a frozen PluginAPI method; incompatible host updates produce HTTP503 instead of a duplicated registry parser. No isolated dependencies are declared. This describes the project's configured location, never a claim about a task's separate execution checkout. Git commands run off the server event loop, with prompts, optional locks, replacement objects and fsmonitor disabled and bounded timeouts. Detached and unborn HEAD remain distinct. The browser requests this only while its disclosure is open; no companion, poller, settings or durable cache exists.

Verification: `python3 -m unittest discover -s tests -v` exercises real temporary ordinary, normal, unborn, detached and linked-worktree folders plus failure/route cases. Use deterministic preflight and the host's actual source-admission/review policy before enablement; verify the installed HTTP route separately.

Bundled source follows normal built-in source trust, admission, grants and enablement. It does not import an external installation or its state.
