"""Dated, read-only configured-project location; no arbitrary-path endpoint."""
import asyncio
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.responses import JSONResponse


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull)
    return env


def git_call(path, *args):
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                           '-C', str(path), *args], env=git_env(),
                          capture_output=True, text=True, timeout=5, check=False)


def inspect_folder(folder):
    result = {'folder': {'status': 'none', 'path': ''},
              'git': {'status': 'not_applicable', 'worktree': '', 'branch': '',
                      'head_state': None, 'commit': None}}
    if not folder:
        return result
    result['folder'] = {'status': 'unavailable', 'path': folder}
    try:
        path = Path(folder)
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            result['folder']['reason'] = 'folder_unavailable'
            return result
        result['folder']['status'] = 'available'
        git = result['git']
        git['status'] = 'unavailable'
        probe = git_call(path, 'rev-parse', '--show-toplevel')
        if probe.returncode:
            if probe.returncode == 128 and 'not a git repository (or any of the parent directories)' in probe.stderr:
                git['status'] = 'not_repository'
            else:
                git['reason'] = 'dubious_ownership' if 'dubious ownership' in probe.stderr else 'git_probe_failed'
            return result
        git.update(status='repository', worktree=probe.stdout.rstrip('\n'), head_state='unknown')
        ref = git_call(path, 'symbolic-ref', '-q', 'HEAD')
        commit = git_call(path, 'rev-parse', '--verify', '-q', 'HEAD^{commit}')
        if commit.returncode == 0:
            git['commit'] = commit.stdout.strip()
        if ref.returncode == 0 and ref.stdout.strip().startswith('refs/heads/'):
            name = ref.stdout.strip()
            git['branch'] = name[len('refs/heads/'):]
            if commit.returncode == 0:
                git['head_state'] = 'branch'
            elif commit.returncode == 1:
                exists = git_call(path, 'show-ref', '--verify', '-q', name)
                if exists.returncode == 1:
                    git['head_state'] = 'unborn'
        elif ref.returncode == 1 and commit.returncode == 0:
            git['head_state'] = 'detached'
        if git['head_state'] == 'unknown':
            git['reason'] = 'head_unavailable'
        return result
    except FileNotFoundError:
        result['git'].update(status='unavailable', reason='git_missing')
    except subprocess.TimeoutExpired:
        result['git'].update(status='unavailable', reason='git_timeout')
    except (OSError, UnicodeError):
        result['git'].update(status='unavailable', reason='inspection_failed')
    return result


def project_info(data_dir, project_id):
    # Deliberate internal dependency: use the installed registry owner, never a
    # copied projects.json parser or an unauthenticated loopback owner request.
    # A host without this reader reports unavailable instead of guessing.
    from ouroboros.projects_registry import get_reserved_project
    project = get_reserved_project(data_dir, project_id, strict=True)
    if not project or project.get('id') != project_id or project.get('lifecycle') != 'active':
        return None
    started = time.monotonic()
    result = inspect_folder(str(project.get('working_dir') or ''))
    result.update(schema_version=1, project_id=project_id,
                  observed_at=datetime.now(timezone.utc).isoformat(),
                  probe_ms=round((time.monotonic() - started) * 1000),
                  basis='project_configured_folder')
    return result


def register(api):
    async def info(request):
        if set(request.query_params) != {'project_id'}:
            return JSONResponse({'error': 'Exactly one project_id is required.'}, status_code=400, headers={'Cache-Control': 'no-store'})
        ids = request.query_params.getlist('project_id')
        if len(ids) != 1 or not ids[0] or len(ids[0]) > 200:
            return JSONResponse({'error': 'One registered project_id is required.'}, status_code=400, headers={'Cache-Control': 'no-store'})
        try:
            data_dir = api.get_runtime_info()['data_dir']
            result = await asyncio.to_thread(project_info, data_dir, ids[0])
        except Exception:
            # Canonical registry errors must never become no-folder/no-Git.
            return JSONResponse({'error': 'Project information unavailable.'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        if result is None:
            return JSONResponse({'error': 'Active project not found.'}, status_code=404, headers={'Cache-Control': 'no-store'})
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    api.register_route('info', handler=info, methods=('GET',))
