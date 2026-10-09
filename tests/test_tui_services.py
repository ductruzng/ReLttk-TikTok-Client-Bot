import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tui_services import (
    Conversation, TIME_PATTERN, SESSION_PATTERN, atomic_write_json,
    list_sessions, matching_selected_ids, parse_inbox, read_json_object,
    save_plan, save_preferences, valid_session_name,
)


class TUIServiceTests(unittest.TestCase):
    def test_sessions_only_json_with_safe_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'duck.json').write_text('{}')
            (root / 'bad..session.json').write_text('{}')
            (root / 'not-json.txt').write_text('{}')
            (root / '.json').write_text('{}')
            self.assertEqual(list_sessions(root), ['duck'])
            self.assertFalse(valid_session_name('../evil'))
            self.assertFalse(valid_session_name('a/b'))
            self.assertTrue(valid_session_name('my_name-1'))

    def test_inbox_parses_real_ids_only(self):
        response = [
            {'conv_id':'a:1', 'conv_short_id':123, 'conv_type':1, 'name':' Friend\nA '},
            {'conv_id':'b:2', 'conv_short_id':456, 'conv_type':2, 'name':'Group'},
            {'conv_id':'b:2', 'conv_short_id':456, 'conv_type':2, 'name':'Duplicate'},
            {'conv_id':'invalid', 'conv_short_id':0, 'conv_type':1, 'name':'No'},
            {'conv_id':'bad', 'conv_short_id':'foo', 'conv_type':1},
            {'conv_id':'c', 'conv_short_id':44, 'conv_type':99},
            {'conv_id':'d', 'conv_short_id':True, 'conv_type':1},
        ]
        rows = parse_inbox(response)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].name, 'Friend A')
        self.assertEqual(rows[1].conv_type, 2)
        self.assertEqual(rows[0].target(), {'conv_id':'a:1','conv_short_id':123,'conv_type':1})

    def test_restore_only_exact_matching_targets(self):
        rows = [Conversation('a', 25, 1, 'A'), Conversation('b', 40, 2, 'B')]
        stored = [
            {'conv_id':'a','conv_short_id':25,'conv_type':1},
            {'conv_id':'b','conv_short_id':41,'conv_type':2},
        ]
        self.assertEqual(matching_selected_ids(stored, rows), {'a'})

    def test_save_plan_preserves_unknown_fields_and_rejects_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'streak.local.json'
            atomic_write_json(path, {'note':'keep', 'session':'old','targets':[{'conv_id':'dummy'}]})
            with self.assertRaises(ValueError):
                save_plan('account','text',[],path)
            self.assertEqual(read_json_object(path)['session'],'old')
            target=Conversation('0:1:10:20', 999, 1, 'Friend')
            obj=save_plan('account',' hello ',[target],path)
            self.assertEqual(obj['note'],'keep')
            self.assertEqual(obj['message'],'hello')
            self.assertEqual(obj['targets'],[target.target()])
            self.assertEqual(read_json_object(path),obj)

    def test_schedule_settings_do_not_start_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'schedule.local.json'
            with self.assertRaises(ValueError):
                save_preferences('26:00', False, path)
            value=save_preferences('08:45', False, path)
            self.assertEqual(value['time'],'08:45')
            self.assertEqual(read_json_object(path),value)

    def test_ui_has_no_send_cli_flag(self):
        code=(Path(__file__).resolve().parents[1] / 'tiktok_tui.py').read_text(encoding='utf-8')
        self.assertNotIn('"--send"',code)
        self.assertNotIn("'--send'",code)
        self.assertIn('"--dry-run"',code)


if __name__=='__main__':
    unittest.main()
