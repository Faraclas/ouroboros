import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

spec = importlib.util.spec_from_file_location('orientation', Path(__file__).parents[1] / 'plugin.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.path), *args], env=m.git_env(),
                              capture_output=True, text=True, check=True).stdout.strip()

    def init(self):
        self.git('init', '-b', 'test-branch')

    def commit(self):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-m', 'test')

    def test_none(self):
        self.assertEqual(m.inspect_folder('')['folder']['status'], 'none')

    def test_ordinary(self):
        result = m.inspect_folder(str(self.path))
        self.assertEqual(result['folder']['status'], 'available')
        self.assertEqual(result['git']['status'], 'not_repository')
        self.assertFalse((self.path / '.git').exists())

    def test_missing(self):
        self.assertEqual(m.inspect_folder(str(self.path / 'missing'))['folder']['status'], 'unavailable')

    def test_unborn(self):
        self.init()
        data = m.inspect_folder(str(self.path))['git']
        self.assertEqual((data['head_state'], data['branch'], data['commit']), ('unborn', 'test-branch', None))

    def test_branch_and_subdir(self):
        self.init(); self.commit()
        child = self.path / 'subdir'; child.mkdir()
        data = m.inspect_folder(str(child))['git']
        self.assertEqual(data['head_state'], 'branch')
        self.assertEqual(data['worktree'], str(self.path))
        self.assertEqual(data['commit'], self.git('rev-parse', 'HEAD'))

    def test_detached(self):
        self.init(); self.commit(); self.git('checkout', '--detach')
        data = m.inspect_folder(str(self.path))['git']
        self.assertEqual(data['head_state'], 'detached')
        self.assertEqual(data['branch'], '')

    def test_linked_worktree(self):
        self.init(); self.commit()
        linked = self.path / 'linked'
        self.git('worktree', 'add', '-b', 'linked-branch', str(linked))
        data = m.inspect_folder(str(linked))['git']
        self.assertEqual((data['worktree'], data['branch']), (str(linked), 'linked-branch'))

    def test_timeout_not_absence(self):
        with patch.object(m, 'git_call', side_effect=subprocess.TimeoutExpired('git', 5)):
            self.assertEqual(m.inspect_folder(str(self.path))['git']['status'], 'unavailable')

    def test_missing_git_not_absence(self):
        with patch.object(m, 'git_call', side_effect=FileNotFoundError()):
            self.assertEqual(m.inspect_folder(str(self.path))['git']['reason'], 'git_missing')

    def test_other_git_error_not_absence(self):
        with patch.object(m, 'git_call', return_value=subprocess.CompletedProcess([],128,'','dubious ownership')):
            self.assertEqual(m.inspect_folder(str(self.path))['git']['status'], 'unavailable')

    def test_broken_git_link_is_unavailable(self):
        (self.path / '.git').write_text('gitdir: ' + str(self.path / 'missing-git-data') + '\n')
        self.assertEqual(m.inspect_folder(str(self.path))['git']['status'], 'unavailable')

    def test_registry_active_only_exact_id(self):
        from ouroboros import projects_registry
        row = {'id':'p', 'lifecycle':'active', 'working_dir':str(self.path)}
        with patch.object(projects_registry, 'get_reserved_project', return_value=row) as reader:
            self.assertEqual(m.project_info('data', 'p')['folder']['status'], 'available')
            reader.assert_called_once_with('data', 'p', strict=True)
        for row in (None, {'id':'p','lifecycle':'tombstoned'}, {'id':'other','lifecycle':'active'}):
            with patch.object(projects_registry, 'get_reserved_project', return_value=row):
                self.assertIsNone(m.project_info('data', 'p'))

    def test_registry_failure_propagates(self):
        from ouroboros import projects_registry
        with patch.object(projects_registry, 'get_reserved_project', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError): m.project_info('data','p')

    def test_environment_scrub(self):
        with patch.dict(m.os.environ, {'GIT_DIR':'wrong', 'GIT_TRACE':'/tmp/wrong'}):
            env = m.git_env()
        self.assertNotIn('GIT_DIR', env); self.assertNotIn('GIT_TRACE', env)
        self.assertEqual(env['GIT_OPTIONAL_LOCKS'], '0')

    def handler(self):
        class API:
            def get_runtime_info(self): return {'data_dir':'unused'}
            def register_route(self, name, handler, methods): self.handler = handler
        api = API(); m.register(api)
        return api.handler

    def request(self, query):
        return Request({'type':'http','method':'GET','path':'/info','query_string':query.encode(),'headers':[]})

    def test_route_no_arbitrary_path(self):
        response = asyncio.run(self.handler()(self.request('project_id=p&path=/tmp')))
        self.assertEqual(response.status_code,400)

    def test_route_registry_failure(self):
        with patch.object(m,'project_info', side_effect=OSError('private')):
            response = asyncio.run(self.handler()(self.request('project_id=p')))
        self.assertEqual(response.status_code,503)
        self.assertNotIn('private', response.body.decode())

    def test_route_success(self):
        with patch.object(m,'project_info',return_value={'schema_version':1,'project_id':'p'}):
            response = asyncio.run(self.handler()(self.request('project_id=p')))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-store')


if __name__ == '__main__': unittest.main()
