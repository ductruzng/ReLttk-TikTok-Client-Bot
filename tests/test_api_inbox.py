import unittest
from unittest.mock import patch, MagicMock
from core.api import get_conversations, get_group_names

class TestApiInbox(unittest.TestCase):
    @patch('core.api.get_user_profiles')
    @patch('core.api.get_own_user_id')
    @patch('core.api._fetch_inbox')
    @patch('core.api._parse_inbox')
    def test_get_conversations_combines_direct_and_group(self, mock_parse, mock_fetch, mock_get_own_uid, mock_get_profiles):
        mock_get_own_uid.return_value = "self_uid"
        mock_get_profiles.return_value = []
        # Setup mock responses
        mock_fetch.side_effect = [b'direct_response', b'group_response']
        
        # Setup mock parsing results
        mock_parse.side_effect = [
            [
                {"conv_id": "direct1", "conv_type": 1, "is_group": False, "name": "user1"},
                {"conv_id": "direct2", "conv_type": 1, "is_group": False, "name": "user2"}
            ],
            [
                {"conv_id": "group1", "conv_type": 2, "is_group": True, "name": "Group A"}
            ]
        ]
        
        # Call function
        result = get_conversations(cookies={}, device_id="test")
        
        # Verify calls
        self.assertEqual(mock_fetch.call_count, 2)
        mock_fetch.assert_any_call(sub_command=10001, field_6=0, cookies={}, device_id="test")
        mock_fetch.assert_any_call(sub_command=10002, field_6=1, cookies={}, device_id="test")
        
        self.assertEqual(mock_parse.call_count, 2)
        mock_parse.assert_any_call(b'direct_response')
        mock_parse.assert_any_call(b'group_response')
        
        # Verify results combined correctly
        self.assertEqual(len(result), 3)
        conv_ids = [c["conv_id"] for c in result]
        self.assertIn("direct1", conv_ids)
        self.assertIn("direct2", conv_ids)
        self.assertIn("group1", conv_ids)

    @patch('core.api.get_user_profiles')
    @patch('core.api.get_own_user_id')
    @patch('core.api._fetch_inbox')
    @patch('core.api._parse_inbox')
    def test_get_conversations_avoids_duplicates(self, mock_parse, mock_fetch, mock_get_own_uid, mock_get_profiles):
        mock_get_own_uid.return_value = "self_uid"
        mock_get_profiles.return_value = []
        mock_fetch.side_effect = [b'direct_response', b'group_response']
        
        # If server mistakenly returns the same conversation in both lists
        mock_parse.side_effect = [
            [{"conv_id": "dup1", "name": "duplicate in direct", "is_group": False}],
            [{"conv_id": "dup1", "name": "duplicate in group", "is_group": False}]
        ]
        
        result = get_conversations()
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["conv_id"], "dup1")
        # Since group is processed second, it will overwrite in the dictionary
        self.assertEqual(result[0]["name"], "duplicate in group")

    @patch('core.api.get_conversations')
    def test_get_group_names(self, mock_get_convs):
        mock_get_convs.return_value = [
            {"conv_id": "group1", "name": "Group One", "is_group": True},
            {"conv_id": "group2", "name": "group2", "is_group": True}, # name == conv_id
            {"conv_id": "direct1", "name": "User", "is_group": False}
        ]
        
        result = get_group_names()
        
        self.assertEqual(len(result), 1)
        self.assertIn("group1", result)
        self.assertEqual(result["group1"], "Group One")
        self.assertNotIn("group2", result)
        self.assertNotIn("direct1", result)

if __name__ == '__main__':
    unittest.main()
