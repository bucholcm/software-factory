import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from factory.cli import main
from factory.config import write_json
from factory.store import Store
from factory.usage import Collector
from scripts.status_panel import usage_lines
from skills.factory.scripts import project as helper


class UsageCLITests(unittest.TestCase):
    def test_project_helper_usage_cannot_submit_or_change_project(self):
        resolved = {'factory_root': '/synthetic/factory', 'project': '/synthetic/project', 'configurations': ['/synthetic/config.json']}
        argv = ['helper', '--project', '/synthetic/project', '--action', 'usage', '--task', 'ticket-1', '--related']
        with patch('sys.argv', argv), patch.object(helper, 'discover', return_value=resolved), patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaises(SystemExit) as result:
                helper.main()
        self.assertEqual(0, result.exception.code)
        args = run.call_args.args[0]
        self.assertIn('usage', args)
        self.assertEqual(['--job', 'ticket-1', '--related'], args[-3:])
        self.assertNotIn('submit', args)

    def test_usage_cli_returns_safe_evidence_without_starting_supervisor(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)/'state'
            config = {'repo': folder, 'state_dir': str(state), 'runtime': 'codex', 'integration_policy': 'disabled'}
            path = Path(folder)/'config.json';write_json(path, config)
            store = Store(state)
            job = store.enqueue('synthetic', folder, 'synthetic', payload={'requirements': 'PRIVATE_REQUIREMENT'})
            attempt = state/'jobs'/job['id']/'implementing-1-100';attempt.mkdir(parents=True)
            collector = Collector(job, 'codex', attempt/'usage.json', config)
            collector.consume({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10, 'reasoning_output_tokens': 0}})
            collector.finish('completed',0)
            output = io.StringIO()
            with patch('sys.argv', ['factory','--config',str(path),'usage','--job','synthetic']), contextlib.redirect_stdout(output), patch('factory.cli.run') as supervisor:
                main()
            supervisor.assert_not_called()
            value=json.loads(output.getvalue())
            self.assertEqual(110,value['summary']['total_tokens'])
            self.assertNotIn('PRIVATE_REQUIREMENT',output.getvalue())

    def test_panel_keeps_unknown_and_runtime_zero_separate_from_estimate(self):
        usage={'coverage':'partial','total_tokens':None,'known_total_tokens':100,
               'estimated_cost':{'status':'partial','currencies':[{'currency':'USD','known_amount':'0.0123'}],
                                 'assumed_models':[{'provider':'synthetic','model':'test'}]},
               'runtime_reported_cost':{'currency':'USD','known_amount':'0','coverage':'partial'},
               'models':[{'runtime':'codex','requested_model':'requested-only','reported_session_model':None}]}
        text='\n'.join(usage_lines(usage))
        self.assertIn('Tokens: 100 (partial)',text)
        self.assertIn('assumed model',text)
        self.assertIn('Runtime cost: 0 USD (partial; not invoice)',text)
        self.assertIn('Observed model: unavailable',text)
        self.assertNotIn('Observed model: requested-only',text)


if __name__ == '__main__':
    unittest.main()
