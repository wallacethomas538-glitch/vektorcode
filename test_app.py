import json, tempfile, unittest
from pathlib import Path
import app
class DashboardTest(unittest.TestCase):
    def test_catalogs(self):
        self.assertEqual({x['id'] for x in app.MODELS},{'local','groq','gemini','openrouter'})
        self.assertIn(('composio','Composio MCP','Connected extended tools'),app.TOOLS)
    def test_receipt_db(self):
        old=app.DB
        with tempfile.TemporaryDirectory() as d:
            app.DB=Path(d)/'t.db'; rid=app.receipt('test',{'tap':True}); self.assertTrue(rid)
        app.DB=old

    def test_shared_vektor_context(self):
        old_root, old_state = app.VEKTOR_ROOT, app.VEKTOR_STATE
        with tempfile.TemporaryDirectory() as d:
            root, state = Path(d), Path(d) / 'state'
            (root).mkdir(exist_ok=True); (state / 'memories').mkdir(parents=True)
            (root / 'SOUL.md').write_text('identity', encoding='utf-8')
            (state / 'memories' / 'MEMORY.md').write_text('memory', encoding='utf-8')
            app.VEKTOR_ROOT, app.VEKTOR_STATE = root, state
            prompt = app.vektor_system_prompt()
            self.assertIn('identity', prompt); self.assertIn('memory', prompt)
        app.VEKTOR_ROOT, app.VEKTOR_STATE = old_root, old_state

    def test_append_shared_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'memory.md'
            app.append_shared_file(target, 'A fact', 'Wallace likes tests.')
            self.assertIn('A fact', target.read_text())
            self.assertIn('Wallace likes tests.', target.read_text())
if __name__=='__main__': unittest.main()
