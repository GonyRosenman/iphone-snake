"""Mutation target: allowing an intent to authorize a different phone operation."""
import unittest
import uuid
from homevoice_broker.intent import make_intent, validate_intent

class IntentTests(unittest.TestCase):
    def test_intent_matches_exact_owner_operation(self):
        args = dict(call_id=str(uuid.uuid4()), nonce=str(uuid.uuid4()), thread='offline-fixed-agent',
                    sdp='v=0\r\no=offline', device_binding='0'*64, release_ref='offline-review-reference')
        intent = make_intent(**args)
        digest = validate_intent(intent, **args)
        self.assertEqual(len(digest), 64)
        for field, changed in [('thread','other'), ('nonce',str(uuid.uuid4())), ('sdp','v=0\r\no=different'),
                               ('call_id',str(uuid.uuid4())), ('device_binding','1'*64), ('release_ref','different-review')]:
            with self.assertRaises(ValueError): validate_intent(intent, **(args|{field: changed}))
        with self.assertRaises(ValueError): validate_intent(intent, **(args|{'release_ref': None}))
        with self.assertRaises(ValueError): validate_intent(intent|{'extra':'unexpected'}, **args)
