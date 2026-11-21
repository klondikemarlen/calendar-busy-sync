import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from calendar_busy_sync.models import SyncError
# isort: split

from calendar_busy_sync.connector_store import Store


class MemoryPeer:
    def __init__(self):
        self.destination = None
        self.calls = []
        self.unavailable = False
        self.lose_reply = False
        self.before_delivery = None

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if self.unavailable:
            raise OSError("synthetic private transport detail")

        if self.before_delivery:
            self.before_delivery(path, payload)

        response = self.destination.peer(path, payload)
        if self.lose_reply:
            self.lose_reply = False
            raise SyncError("synthetic private transport detail")

        return response


class ConnectorStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = self.enterContext(patch("calendar_busy_sync.connector_store.time"))
        self.clock.time.return_value = 1_000_000
        self.work_peer = MemoryPeer()
        self.personal_peer = MemoryPeer()
        self.work = self.open_store("microsoft", self.work_peer)
        self.personal = self.open_store("google", self.personal_peer)
        self.work_peer.destination = self.personal
        self.personal_peer.destination = self.work
        self.work.enroll("work-one", "work-grant-one")
        self.personal.enroll("personal-one", "personal-grant-one")

    def open_store(self, role, peer):
        db = sqlite3.connect(self.root / f"{role}.sqlite3", isolation_level=None)
        self.addCleanup(db.close)
        return Store(SimpleNamespace(role=role), db, peer)

    def pair(self, work="work-one", personal="personal-one"):
        invitation = self.work.offer(work)
        confirmation = self.personal.join(personal, invitation)
        self.work.confirm(work, confirmation)
        return self.work.account(work)[0]

    def test_second_consent_activates_the_same_pair_at_both_connectors(self):
        invitation = self.work.offer("work-one")
        confirmation = self.personal.join("personal-one", invitation)

        self.assertNotEqual(invitation, confirmation)
        self.assertEqual(self.work.active_pairs(), [])
        self.assertEqual(self.personal.active_pairs(), [])
        with self.assertRaises(SyncError):
            self.personal.grant(self.personal.account("personal-one")[0])

        self.work.confirm("work-one", confirmation)

        pair_id, _ = self.work.account("work-one")
        self.assertRegex(pair_id, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(self.work.active_pairs(), [pair_id])
        self.assertEqual(self.personal.active_pairs(), [pair_id])

    def test_pairing_peer_payloads_do_not_contain_identity_or_grants(self):
        pair_id = self.pair()

        ((claim_path, claim),) = self.personal_peer.calls
        self.assertEqual(claim_path, "/peer/claim")
        self.assertEqual(set(claim), {"code", "pair_id", "confirmation_digest"})
        self.assertEqual(claim["pair_id"], pair_id)
        self.assertRegex(claim["confirmation_digest"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(self.work_peer.calls, [("/peer/activate", {"pair_id": pair_id})])

    def test_unenrolled_google_subject_cannot_redeem_an_invitation(self):
        invitation = self.work.offer("work-one")

        with self.assertRaises(SyncError):
            self.personal.join("unknown", invitation)

        self.assertEqual(self.personal_peer.calls, [])

    def test_another_google_subject_cannot_replay_a_used_invitation(self):
        self.personal.enroll("personal-two", "personal-grant-two")
        invitation = self.work.offer("work-one")
        confirmation = self.personal.join("personal-one", invitation)

        with self.assertRaises(SyncError):
            self.personal.join("personal-two", invitation)

        self.work.confirm("work-one", confirmation)
        self.assertEqual(self.personal.account("personal-two"), (None, False))

    def test_confirmation_cannot_activate_a_different_microsoft_subject(self):
        self.work.enroll("work-two", "work-grant-two")
        self.personal.enroll("personal-two", "personal-grant-two")
        first_confirmation = self.personal.join("personal-one", self.work.offer("work-one"))
        self.personal.join("personal-two", self.work.offer("work-two"))

        with self.assertRaises(SyncError):
            self.work.confirm("work-two", first_confirmation)

        self.assertEqual(self.work_peer.calls, [])

    def test_grant_updates_and_same_subject_reenrollment_preserve_pair_isolation(self):
        self.work.enroll("work-two", "work-grant-two")
        self.personal.enroll("personal-two", "personal-grant-two")
        first_pair = self.pair()
        second_pair = self.pair("work-two", "personal-two")

        self.work.enroll("work-one", "new-work-grant")
        self.personal.save_grant(first_pair, "refreshed-personal-grant")

        self.assertNotEqual(first_pair, second_pair)
        self.assertEqual(self.work.grant(first_pair), "new-work-grant")
        self.assertEqual(self.personal.grant(first_pair), "refreshed-personal-grant")
        self.assertEqual(self.work.grant(second_pair), "work-grant-two")
        self.assertEqual(self.personal.grant(second_pair), "personal-grant-two")

    def test_invitation_expires_before_it_can_be_claimed(self):
        invitation = self.work.offer("work-one")
        self.clock.time.return_value += 601

        with self.assertRaises(SyncError):
            self.personal.join("personal-one", invitation)

        self.assertEqual(self.work.account("work-one"), (None, False))

    def test_expired_confirmation_cannot_activate_a_pair(self):
        confirmation = self.personal.join("personal-one", self.work.offer("work-one"))
        self.clock.time.return_value += 601

        with self.assertRaises(SyncError):
            self.work.confirm("work-one", confirmation)

        self.assertEqual(self.work_peer.calls, [])

    def test_confirmation_can_recover_from_a_lost_activation_acknowledgement(self):
        confirmation = self.personal.join("personal-one", self.work.offer("work-one"))
        self.work_peer.lose_reply = True

        with self.assertRaisesRegex(SyncError, "did not complete pairing"):
            self.work.confirm("work-one", confirmation)
        self.assertEqual(self.work.active_pairs(), [])

        self.work.confirm("work-one", confirmation)

        self.assertEqual(self.work.active_pairs(), self.personal.active_pairs())
        with self.assertRaises(SyncError):
            self.work.confirm("work-one", confirmation)

    def test_lost_claim_acknowledgement_is_unlinked_by_the_worker(self):
        self.personal_peer.lose_reply = True
        invitation = self.work.offer("work-one")

        with self.assertRaisesRegex(SyncError, "did not complete pairing"):
            self.personal.join("personal-one", invitation)
        self.assertEqual(self.personal.account("personal-one"), (None, False))

        self.personal.retry_disconnects()

        self.assertEqual(self.work.account("work-one"), (None, False))

    def test_worker_unlinks_an_uncertain_activation_after_confirmation_expires(self):
        confirmation = self.personal.join("personal-one", self.work.offer("work-one"))
        self.work_peer.lose_reply = True
        with self.assertRaises(SyncError):
            self.work.confirm("work-one", confirmation)
        self.clock.time.return_value += 601

        self.work.retry_disconnects()

        self.assertEqual(self.personal.active_pairs(), [])

    def test_local_disconnect_revokes_access_before_it_contacts_an_unavailable_peer(self):
        pair_id = self.pair()
        observed = []

        def unavailable_peer(path, payload):
            observed.append((self.work.db.in_transaction, self.work.account("work-one")))
            raise OSError("synthetic private transport detail")

        self.work_peer.before_delivery = unavailable_peer
        self.work.disconnect("work-one")

        self.assertEqual(observed, [(False, None)])
        with self.assertRaises(SyncError):
            self.work.grant(pair_id)
        with self.assertRaises(SyncError):
            self.work.save_grant(pair_id, "stale-refresh")

    def test_pending_disconnect_survives_reopening_the_database(self):
        self.pair()
        self.work_peer.unavailable = True
        self.work.disconnect("work-one")
        self.work.db.close()
        self.work = self.open_store("microsoft", self.work_peer)
        self.work_peer.unavailable = False
        self.work_peer.lose_reply = True

        self.work.retry_disconnects()
        self.work.retry_disconnects()
        self.work.retry_disconnects()

        self.assertEqual(self.personal.account("personal-one"), (None, False))
        self.assertEqual([path for path, _ in self.work_peer.calls].count("/peer/disconnect"), 3)

    def test_worker_limits_notices_per_pass_and_makes_progress_on_the_backlog(self):
        pair_ids = []
        for number in range(9):
            work = f"work-batch-{number}"
            personal = f"personal-batch-{number}"
            self.work.enroll(work, "work-grant")
            self.personal.enroll(personal, "personal-grant")
            pair_ids.append(self.pair(work, personal))
            self.work_peer.unavailable = True
            self.work.disconnect(work)
            self.work_peer.unavailable = False
        self.work_peer.calls.clear()
        self.work_peer.unavailable = True

        self.work.retry_disconnects()
        first_batch = {payload["pair_id"] for _, payload in self.work_peer.calls}
        self.work_peer.calls.clear()
        self.work.retry_disconnects()
        second_batch = {payload["pair_id"] for _, payload in self.work_peer.calls}

        self.assertEqual(len(first_batch), 8)
        self.assertEqual(first_batch | second_batch, set(pair_ids))

    def test_old_invitation_cannot_pair_a_reenrolled_microsoft_subject(self):
        old_invitation = self.work.offer("work-one")
        self.work.disconnect("work-one")
        self.work.enroll("work-one", "new-work-grant")

        with self.assertRaises(SyncError):
            self.personal.join("personal-one", old_invitation)

        self.assertEqual(self.work.account("work-one"), (None, False))

    def test_delayed_claim_for_an_already_unlinked_pair_is_rejected(self):
        invitation = self.work.offer("work-one")

        def disconnect_before_delivery(path, payload):
            self.personal_peer.before_delivery = None
            self.personal.disconnect("personal-one")
            self.personal.enroll("personal-one", "new-personal-grant")

        self.personal_peer.before_delivery = disconnect_before_delivery
        with self.assertRaises(SyncError):
            self.personal.join("personal-one", invitation)

        self.assertEqual(self.work.account("work-one"), (None, False))

    def test_old_activation_cannot_reactivate_a_reenrolled_google_subject(self):
        pair_id = self.pair()
        self.personal.disconnect("personal-one")
        self.personal.enroll("personal-one", "new-personal-grant")
        new_pair = self.pair()

        with self.assertRaises(SyncError):
            self.personal.peer("/peer/activate", {"pair_id": pair_id})

        self.assertEqual(self.personal.active_pairs(), [new_pair])

    def test_old_confirmation_cannot_activate_a_new_pair_after_disconnect(self):
        old_confirmation = self.personal.join("personal-one", self.work.offer("work-one"))
        self.work.disconnect("work-one")
        self.work.enroll("work-one", "new-work-grant")
        self.personal.join("personal-one", self.work.offer("work-one"))

        with self.assertRaises(SyncError):
            self.work.confirm("work-one", old_confirmation)

        self.assertEqual(self.work.active_pairs(), [])

    def test_late_disconnect_cannot_affect_a_new_active_pair(self):
        old_pair_id = self.pair()
        self.work.disconnect("work-one")
        self.work.enroll("work-one", "new-work-grant")
        new_pair_id = self.pair()

        self.personal.peer("/peer/disconnect", {"pair_id": old_pair_id})

        self.assertEqual(self.personal.active_pairs(), [new_pair_id])

    def test_peer_endpoints_reject_extra_properties_unknown_routes_and_wrong_roles(self):
        pair_id = self.pair()

        with self.assertRaises(SyncError):
            self.personal.peer("/peer/disconnect", {"pair_id": pair_id, "subject": "personal-one"})
        with self.assertRaises(SyncError):
            self.personal.peer("/peer/account", {"subject": "personal-one"})
        with self.assertRaises(SyncError):
            self.work.peer("/peer/activate", {"pair_id": pair_id})
        with self.assertRaises(SyncError):
            self.personal.peer("/peer/disconnect", {"pair_id": [pair_id]})
