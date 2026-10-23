"""Local request authorization, finalization, and migration without model calls."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from agent_bridge import interactive, interactive_guard as guard, management


class LocalRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.path = self.project / 'state' / 'interactive.sqlite3'
        self.api = management.Session()
        self.addCleanup(self.api.close)

    def request(self, action, *, api=None, **args):
        return (api or self.api).handle(dict(
            protocol=1, id=str(uuid.uuid4()), action=action, args=args))

    def call(self, action, **args):
        response = self.request(action, **args)
        self.assertTrue(response['ok'], response)
        return response['result']

    def scope(self, **extra):
        return dict(database=str(self.path), project=str(self.project), **extra)

    def mutation(self, session_id='local-session', owner='owner', **extra):
        return dict(database=str(self.path), owner=owner, session_id=session_id,
                    apply=True, **extra)

    def open_owner(self, owner='owner', project=None):
        return self.call('interactive.owner.open', database=str(self.path),
                         project=str(project or self.project), owner=owner, apply=True)

    def begin(self, session_id='local-session', owner='owner', provider='local', project=None):
        return self.call('interactive.begin', database=str(self.path),
                         project=str(project or self.project), session_id=session_id,
                         owner=owner, provider=provider, node='node-' + session_id,
                         name='Fixture session', apply=True)

    def record(self, kind, data, session_id='local-session', owner='owner'):
        return self.call('interactive.record', **self.mutation(session_id, owner),
                         event_id=str(uuid.uuid4()), kind=kind, data=data)

    def end(self, owner='owner'):
        return self.call('interactive.end', database=str(self.path), owner=owner, apply=True)

    def schema_two(self):
        self.open_owner('bootstrap')
        self.end('bootstrap')
        self.assertEqual(self.version(), 2)

    def upgrade(self):
        preview = self.call('local.schema.preview', **self.scope())
        self.assertTrue(preview['report']['eligible'], preview)
        result = self.call('local.schema.apply', plan_id=preview['plan_id'], apply=True)
        self.assertEqual(self.version(), 3)
        return result

    def ready(self):
        self.schema_two()
        self.upgrade()
        self.open_owner()
        self.begin()

    def version(self):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT version FROM interactive_meta WHERE id=1').fetchone()[0]

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()

    def payload(self, prompt='hello', context=None, **binding_updates):
        body = json.dumps(dict(model='fixture', messages=[
            *(context or []), dict(role='user', content=prompt)], stream=True,
            options=dict(num_ctx=4096, num_predict=64)), ensure_ascii=False,
            separators=(',', ':'))
        binding = dict(helper_id='helper', generation='generation', core_epoch='epoch',
                       profile_revision='revision', backend='ollama',
                       endpoint_origin='http://127.0.0.1:11434', model_id='fixture',
                       body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(),
                       origin='human', delivery_id=None, root=None, hops=0)
        binding.update(binding_updates)
        return binding, body

    def prepare(self, request_id=None, session_id='local-session', owner='owner',
                prompt='hello', context=None, **binding_updates):
        request_id = request_id or str(uuid.uuid4())
        binding, body = self.payload(prompt, context, **binding_updates)
        result = self.call('local.prepare', **self.mutation(session_id, owner),
                           request_id=request_id, binding=binding, request_body=body)
        return request_id, result['binding_sha256']

    def dispatch(self, request_id, binding_sha256, session_id='local-session', owner='owner'):
        return self.call('local.dispatch', **self.mutation(session_id, owner),
                         request_id=request_id, binding_sha256=binding_sha256)

    def outcome(self, state='COMPLETED', content='fixture reply', usage=None, **extra):
        result = dict(state=state, reason='STOP' if state == 'COMPLETED' else state,
                      content=content, reported_model='fixture', usage=usage or {})
        result.update(extra)
        return result

    def finish(self, request_id, binding_sha256, outcome=None,
               session_id='local-session', owner='owner'):
        return self.call('local.finish', **self.mutation(session_id, owner),
                         request_id=request_id, binding_sha256=binding_sha256,
                         outcome=outcome or self.outcome())

    def result(self, request_id, **extra):
        return self.call('local.result', **self.scope(request_id=request_id), **extra)

    def context(self, session_id='local-session'):
        return self.call('local.context', **self.scope(session_id=session_id))['messages']

    def test_schema_upgrade_is_explicit_and_preserves_cli_rows(self):
        self.open_owner('old')
        self.begin('cli', 'old', 'claude')
        self.record('completed', dict(answer='Existing CLI answer'), 'cli', 'old')
        self.record('usage', dict(sequence=1, metrics=dict(input_tokens=7), source='fixture'), 'cli', 'old')
        self.record('session_stopped', dict(confirmed_exit=True), 'cli', 'old')
        self.end('old')
        tables = ('interactive_sessions', 'interactive_events', 'interactive_usage',
                  'interactive_owners', 'interactive_session_control')
        before = {table: self.rows(table) for table in tables}
        old_connection = sqlite3.connect(self.path)
        self.addCleanup(old_connection.close)
        preview = self.call('local.schema.preview', **self.scope())
        self.assertEqual(self.version(), 2)
        self.assertTrue(preview['report']['eligible'])
        refused = self.request('local.schema.apply', plan_id=preview['plan_id'], apply=False)
        self.assertFalse(refused['ok'])
        self.assertEqual(self.version(), 2)
        self.call('local.schema.apply', plan_id=preview['plan_id'], apply=True)
        self.assertEqual(self.version(), 3)
        self.assertEqual(before, {table: self.rows(table) for table in tables})
        with self.assertRaises(sqlite3.Error):
            old_connection.execute("UPDATE interactive_sessions SET name='forged'")
        old_connection.rollback()
        self.assertEqual(before, {table: self.rows(table) for table in tables})

    def test_schema_upgrade_checks_foreign_project_active_and_lost_owners(self):
        self.schema_two()
        other = self.project / 'other'
        other.mkdir()
        self.open_owner('foreign', other)
        self.begin('foreign-session', 'foreign', 'codex', other)
        active = self.call('local.schema.preview', **self.scope())
        self.assertFalse(active['report']['eligible'])
        self.assertIn('WRITER_ACTIVE', active['report']['blockers'])
        self.end('foreign')
        lost = self.call('local.schema.preview', **self.scope())
        self.assertFalse(lost['report']['eligible'])
        self.assertIn('WRITER_LOST', lost['report']['blockers'])
        self.assertEqual(self.version(), 2)

    def test_schema_plan_rechecks_owner_opened_after_preview(self):
        self.schema_two()
        preview = self.call('local.schema.preview', **self.scope())
        self.open_owner('late')
        response = self.request('local.schema.apply', plan_id=preview['plan_id'], apply=True)
        self.assertFalse(response['ok'], response)
        self.assertEqual(self.version(), 2)

    def test_schema_two_cannot_start_local_session(self):
        self.schema_two()
        self.open_owner()
        response = self.request('interactive.begin', **self.scope(
            session_id='local-session', node='node', provider='local', name='Local',
            owner='owner', apply=True))
        self.assertFalse(response['ok'], response)
        self.assertEqual(self.rows('interactive_sessions'), [])

    def test_prepare_is_idempotent_but_rejects_changed_identity_or_body(self):
        self.ready()
        request_id, digest = self.prepare()
        again_id, again_digest = self.prepare(request_id)
        self.assertEqual((again_id, again_digest), (request_id, digest))
        for changes in (dict(prompt='changed'), dict(generation='other'),
                        dict(core_epoch='other'), dict(profile_revision='other')):
            with self.subTest(changes=changes):
                binding, body = self.payload(**changes)
                response = self.request('local.prepare', **self.mutation(),
                                        request_id=request_id, binding=binding, request_body=body)
                self.assertFalse(response['ok'], response)
        self.assertEqual(self.result(request_id)['state'], 'PREPARED')

    def test_prepare_rejects_body_hash_mismatch_without_reserving_request(self):
        self.ready()
        request_id = str(uuid.uuid4())
        binding, body = self.payload()
        binding['body_sha256'] = '0' * 64
        response = self.request('local.prepare', **self.mutation(), request_id=request_id,
                                binding=binding, request_body=body)
        self.assertFalse(response['ok'])
        self.prepare(request_id)

    def test_one_inflight_request_per_session_without_blocking_other_sessions(self):
        self.ready()
        request_id, digest = self.prepare()
        binding, body = self.payload('second')
        response = self.request('local.prepare', **self.mutation(),
                                request_id=str(uuid.uuid4()), binding=binding, request_body=body)
        self.assertFalse(response['ok'], response)
        self.begin('other-local')
        self.prepare(session_id='other-local')
        self.finish(request_id, digest, self.outcome('CANCELLED', content=''))
        self.prepare(prompt='new explicit request')

    def test_dispatch_grant_is_never_reissued_after_response_loss(self):
        self.ready()
        request_id, digest = self.prepare()
        granted = self.dispatch(request_id, digest)
        self.assertTrue(granted.get('grant'), granted)
        again = self.request('local.dispatch', **self.mutation(),
                             request_id=request_id, binding_sha256=digest)
        self.assertFalse(again['ok'] and bool((again['result'] or {}).get('grant')), again)
        self.assertEqual(self.result(request_id)['state'], 'DISPATCH_AUTHORIZED')
        self.assertEqual(self.context(), [])

    def test_completed_requires_dispatch_and_commits_context_exactly_once(self):
        self.ready()
        request_id, digest = self.prepare(prompt='one question')
        premature = self.request('local.finish', **self.mutation(), request_id=request_id,
                                 binding_sha256=digest, outcome=self.outcome())
        self.assertFalse(premature['ok'], premature)
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        repeated = self.finish(request_id, digest)
        self.assertTrue(repeated['duplicate'], repeated)
        self.assertEqual(self.context(), [dict(role='user', content='one question'),
                                         dict(role='assistant', content='fixture reply')])
        self.assertEqual(self.result(request_id)['state'], 'COMPLETED')

    def test_cancel_wins_over_late_completion_and_cannot_reanimate_context(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        self.finish(request_id, digest, self.outcome('CANCELLED', content='partial'))
        self.request('local.finish', **self.mutation(), request_id=request_id,
                     binding_sha256=digest, outcome=self.outcome())
        self.assertEqual(self.result(request_id)['state'], 'CANCELLED')
        self.assertEqual(self.context(), [])

    def test_completion_wins_over_late_cancel(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        before = self.result(request_id)
        self.request('local.finish', **self.mutation(), request_id=request_id,
                     binding_sha256=digest, outcome=self.outcome('CANCELLED', content=''))
        self.assertEqual(self.result(request_id), before)
        self.assertEqual(len(self.context()), 2)

    def test_racing_completion_and_cancel_commit_only_one_terminal_outcome(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        barrier = threading.Barrier(2)

        def finalize(outcome):
            barrier.wait(timeout=10)
            return self.request('local.finish', **self.mutation(), request_id=request_id,
                                binding_sha256=digest, outcome=outcome)

        with ThreadPoolExecutor(max_workers=2) as workers:
            completion = workers.submit(finalize, self.outcome())
            cancellation = workers.submit(finalize, self.outcome('CANCELLED', content=''))
            responses = [completion.result(timeout=20), cancellation.result(timeout=20)]
        self.assertTrue(any(response['ok'] for response in responses), responses)
        state = self.result(request_id)['state']
        self.assertIn(state, ('COMPLETED', 'CANCELLED'))
        self.assertEqual(len(self.context()), 2 if state == 'COMPLETED' else 0)
        before = self.result(request_id)
        self.request('local.finish', **self.mutation(), request_id=request_id,
                     binding_sha256=digest, outcome=self.outcome('FAILED', content='late'))
        self.assertEqual(self.result(request_id), before)

    def test_non_completed_user_and_partial_answer_never_enter_next_context(self):
        self.ready()
        for state in ('FAILED', 'TRUNCATED', 'CANCELLED', 'UNCERTAIN'):
            with self.subTest(state=state):
                request_id, digest = self.prepare(prompt='discard ' + state)
                self.dispatch(request_id, digest)
                self.finish(request_id, digest, self.outcome(state, content='partial ' + state))
                self.assertEqual(self.context(), [])
        request_id, digest = self.prepare(prompt='keep this')
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        self.assertEqual(self.context(), [dict(role='user', content='keep this'),
                                         dict(role='assistant', content='fixture reply')])

    def test_foreign_connection_owner_session_and_hash_cannot_dispatch_or_finish(self):
        self.ready()
        self.open_owner('other-owner')
        self.begin('other-session', 'other-owner')
        request_id, digest = self.prepare()
        foreign = management.Session()
        self.addCleanup(foreign.close)
        for action in ('local.dispatch', 'local.finish'):
            for extra in (dict(api=foreign), dict(owner='other-owner', session_id='other-session'),
                          dict(session_id='other-session'), dict(binding_sha256='0' * 64)):
                with self.subTest(action=action, extra=extra):
                    args = self.mutation(request_id=request_id, binding_sha256=digest)
                    args.update(extra)
                    if action == 'local.finish':
                        args['outcome'] = self.outcome()
                    self.assertFalse(self.request(action, **args)['ok'])
        self.assertEqual(self.result(request_id)['state'], 'PREPARED')

    def test_foreign_project_cannot_read_local_request_or_context(self):
        self.ready()
        request_id, digest = self.prepare()
        other = self.project / 'other'
        other.mkdir()
        for action, extra in (('local.result', dict(request_id=request_id)),
                              ('local.context', dict(session_id='local-session'))):
            response = self.request(action, database=str(self.path), project=str(other), **extra)
            self.assertFalse(response['ok'], response)
        self.assertEqual(self.result(request_id)['state'], 'PREPARED')

    def test_generic_completed_and_usage_cannot_bypass_local_request_control(self):
        self.ready()
        before = self.rows('interactive_events')
        for kind, data in (('completed', dict(answer='forged')),
                           ('usage', dict(sequence=1, metrics=dict(input_tokens=999)))):
            response = self.request('interactive.record', **self.mutation(),
                                    event_id=str(uuid.uuid4()), kind=kind, data=data)
            self.assertFalse(response['ok'], response)
        self.assertEqual(self.rows('interactive_events'), before)
        self.assertEqual(self.rows('interactive_usage'), [])
        self.assertEqual(self.context(), [])

    def test_owner_loss_preserves_uncertainty_and_never_reissues_grant(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        self.api.close()
        reader = management.Session()
        self.addCleanup(reader.close)
        self.assertEqual(self.result(request_id, api=reader)['state'], 'UNCERTAIN')
        response = self.request('local.dispatch', api=reader, **self.mutation(),
                                request_id=request_id, binding_sha256=digest)
        self.assertFalse(response['ok'], response)
        self.assertEqual(self.context(), [])

    def test_request_usage_is_accumulated_once_and_unknown_is_not_zero(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        outcome = self.outcome(usage=dict(input_tokens=2, output_tokens=3))
        self.finish(request_id, digest, outcome)
        self.finish(request_id, digest, outcome)
        request_id, digest = self.prepare(prompt='second question', context=self.context())
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        report = self.call('interactive.report', **self.scope())
        metrics = report['summary']['metrics']
        self.assertEqual(metrics['input_tokens']['known_sum'], 2)
        self.assertEqual(metrics['output_tokens']['known_sum'], 3)
        self.assertGreater(metrics['input_tokens']['unknown_tasks'], 0)
        self.assertGreater(metrics['cost_usd']['unknown_tasks'], 0)

    def test_completed_history_cannot_be_silently_dropped_or_replaced(self):
        self.ready()
        request_id, digest = self.prepare(prompt='first question')
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        for previous in ([], [dict(role='user', content='forged history'),
                              dict(role='assistant', content='forged answer')]):
            binding, body = self.payload('second question', context=previous)
            response = self.request('local.prepare', **self.mutation(),
                                    request_id=str(uuid.uuid4()), binding=binding,
                                    request_body=body)
            self.assertFalse(response['ok'], response)
        self.prepare(prompt='second question', context=self.context())

    def test_events_reference_request_instead_of_duplicating_context_bodies(self):
        self.ready()
        prompt = 'UNIQUE_PRIVATE_INPUT_BODY'
        answer = 'UNIQUE_PRIVATE_OUTPUT_BODY'
        request_id, digest = self.prepare(prompt=prompt)
        self.dispatch(request_id, digest)
        self.finish(request_id, digest, self.outcome(content=answer))
        events = json.dumps(self.rows('interactive_events'))
        self.assertNotIn(prompt, events)
        self.assertNotIn(answer, events)
        self.assertEqual(self.context(), [dict(role='user', content=prompt),
                                         dict(role='assistant', content=answer)])

    def test_completed_model_mismatch_cannot_enter_context(self):
        self.ready()
        request_id, digest = self.prepare()
        self.dispatch(request_id, digest)
        self.request('local.finish', **self.mutation(), request_id=request_id,
                     binding_sha256=digest, outcome=self.outcome(reported_model='unexpected-model'))
        self.assertNotEqual(self.result(request_id)['state'], 'COMPLETED')
        self.assertEqual(self.context(), [])

    def test_reopening_the_same_owner_reports_the_actual_schema(self):
        self.ready()
        repeated = self.call('interactive.owner.open', database=str(self.path),
                             project=str(self.project), owner='owner', apply=True)
        self.assertEqual(repeated['schema'], 3)
        self.assertEqual(repeated['writer_protocol'], 2)

    def test_peer_request_cannot_replace_the_recorded_delivery_text(self):
        self.ready()
        self.begin('source', provider='codex')
        delivery = dict(id='delivery', source='node-source', target='node-local-session',
                        source_session='source', target_session='local-session',
                        project=str(self.project), generation='generation', root='root',
                        hops=1, text='Original peer reply', status='queued', reason='')
        self.call('interactive.delivery', database=str(self.path), owner='owner',
                  data=delivery, apply=True)
        binding, body = self.payload('Unrelated input', origin='peer',
                                     delivery_id='delivery', root='root', hops=1)
        response = self.request('local.prepare', **self.mutation(),
                                request_id=str(uuid.uuid4()), binding=binding,
                                request_body=body)
        self.assertFalse(response['ok'], response)
        self.assertEqual(self.rows('interactive_local_requests'), [])
        self.assertEqual(self.rows('interactive_deliveries')[0][2], 'queued')
        request_id, digest = self.prepare(prompt=delivery['text'], origin='peer',
                                          delivery_id='delivery', root='root', hops=1)
        self.dispatch(request_id, digest)
        self.finish(request_id, digest)
        self.assertEqual(self.rows('interactive_deliveries')[0][2], 'completed')
        self.assertEqual(self.context()[0]['content'], delivery['text'])

    def test_dispatch_rechecks_cancelled_peer_delivery_without_reviving_it(self):
        self.ready()
        self.begin('source', provider='codex')
        delivery = dict(id='delivery', source='node-source', target='node-local-session',
                        source_session='source', target_session='local-session',
                        project=str(self.project), generation='generation', root='root',
                        hops=1, text='Peer reply', status='queued', reason='')
        self.call('interactive.delivery', database=str(self.path), owner='owner', data=delivery, apply=True)
        rid, bound = self.prepare(prompt=delivery['text'], origin='peer', delivery_id='delivery', root='root', hops=1)
        response = self.request('interactive.delivery', database=str(self.path), owner='owner',
                                data=dict(delivery, status='cancelled'), apply=True)
        self.assertFalse(response['ok'], response)
        with guard.connection(self.path, '@admin') as db:
            db.execute("UPDATE interactive_deliveries SET status='cancelled',data=? WHERE id='delivery'",
                       (json.dumps(dict(delivery, status='cancelled')),))
        response = self.request('local.dispatch', **self.mutation(), request_id=rid, binding_sha256=bound)
        self.assertFalse(response['ok'], response)
        self.assertEqual(self.result(rid)['state'], 'PREPARED')
        self.assertFalse(self.result(rid)['dispatched'])
        self.record('session_stopped', dict(confirmed_exit=True))
        self.assertEqual(self.result(rid)['state'], 'CANCELLED')
        self.assertEqual(self.rows('interactive_deliveries')[0][2], 'cancelled')

    def test_unconfirmed_session_exit_blocks_new_prepare_and_existing_dispatch(self):
        self.ready()
        request_id, digest = self.prepare()
        self.record('session_stopped', dict(confirmed_exit=False))
        session_before = self.rows('interactive_sessions')
        self.assertEqual(session_before[0][-1], 'UNCERTAIN')
        binding, body = self.payload('Question after unconfirmed exit')
        prepared = self.request('local.prepare', **self.mutation(),
                                request_id=str(uuid.uuid4()), binding=binding,
                                request_body=body)
        self.assertFalse(prepared['ok'], prepared)
        dispatched = self.request('local.dispatch', **self.mutation(),
                                  request_id=request_id, binding_sha256=digest)
        self.assertFalse(dispatched['ok'], dispatched)
        result = self.result(request_id)
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertFalse(result['dispatched'])
        self.assertEqual(len(self.rows('interactive_local_requests')), 1)
        self.assertEqual(self.rows('interactive_sessions'), session_before)

    def test_allowed_alias_preserves_the_reported_model_and_usage_source(self):
        self.ready()
        alias = 'PRIVATE_CANONICAL_MODEL'
        request_id, digest = self.prepare(model_aliases=[alias])
        self.dispatch(request_id, digest)
        self.finish(request_id, digest, self.outcome(
            reported_model=alias, content='Reply from the canonical model',
            usage=dict(input_tokens=7, output_tokens=9)))
        result = self.result(request_id)
        self.assertEqual(result['state'], 'COMPLETED')
        self.assertEqual(result['binding']['model_id'], 'fixture')
        self.assertEqual(result['reported_model'], alias)
        self.assertEqual(result['content'], 'Reply from the canonical model')
        self.assertEqual(result['usage']['requested_model_ref'],
                         hashlib.sha256(b'fixture').hexdigest())
        self.assertEqual(result['usage']['reported_model_ref'],
                         hashlib.sha256(alias.encode()).hexdigest())
        self.assertEqual(self.context()[-1], dict(
            role='assistant', content='Reply from the canonical model'))

    def test_model_mismatch_retains_provenance_and_purge_removes_model_names(self):
        self.ready()
        requested, reported = 'PRIVATE_REQUESTED_MODEL', 'PRIVATE_REPORTED_MODEL'
        binding, body = self.payload()
        payload = json.loads(body)
        payload['model'] = requested
        body = json.dumps(payload, separators=(',', ':'))
        binding.update(model_id=requested,
                       body_sha256=hashlib.sha256(body.encode()).hexdigest())
        request_id = str(uuid.uuid4())
        prepared = self.call('local.prepare', **self.mutation(),
                             request_id=request_id, binding=binding, request_body=body)
        digest = prepared['binding_sha256']
        self.dispatch(request_id, digest)
        self.finish(request_id, digest, self.outcome(
            reported_model=reported, usage=dict(input_tokens=2, output_tokens=3)))
        before = self.result(request_id)
        self.assertEqual(before['state'], 'FAILED')
        self.assertEqual(before['reason'], 'UNSUPPORTED_RESPONSE')
        self.assertEqual(before['reported_model'], reported)
        self.assertEqual(before['usage']['input_tokens'], 2)
        self.assertEqual(before['usage']['output_tokens'], 3)
        self.assertEqual(before['usage']['requested_model_ref'],
                         hashlib.sha256(requested.encode()).hexdigest())
        self.assertEqual(before['usage']['reported_model_ref'],
                         hashlib.sha256(reported.encode()).hexdigest())
        self.assertEqual(self.context(), [])
        for value in (requested, reported):
            self.assertNotIn(value, json.dumps(self.rows('interactive_local_usage')))
        self.record('session_stopped', dict(confirmed_exit=True))
        self.end()
        preview = self.call('history.purge.preview',
                            **self.scope(session_ids=['local-session']))
        self.assertTrue(preview['report']['eligible'], preview)
        self.call('history.apply', plan_id=preview['plan_id'], apply=True)
        after = self.result(request_id)
        self.assertTrue(after['body_deleted'])
        self.assertNotIn('reported_model', after)
        self.assertNotIn('content', after)
        self.assertEqual(after['usage'], before['usage'])
        tables = ('interactive_local_requests', 'interactive_local_bodies',
                  'interactive_local_usage', 'interactive_sessions', 'interactive_events',
                  'interactive_usage', 'interactive_redactions')
        serialized = json.dumps([self.rows(table) for table in tables])
        for value in (requested, reported):
            self.assertNotIn(value, serialized)

    def test_local_request_lifecycle_never_opens_network_or_invokes_provider(self):
        with patch('socket.create_connection', side_effect=AssertionError('Network is not allowed')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('HTTP is not allowed')), \
                patch('subprocess.Popen', side_effect=AssertionError('Providers are not allowed')):
            self.ready()
            request_id, digest = self.prepare()
            self.dispatch(request_id, digest)
            self.finish(request_id, digest)
            self.assertEqual(self.result(request_id)['state'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
