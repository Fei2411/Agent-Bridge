import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { test } from 'node:test';
import { fixture, call, stop, stream, agent, cli } from './helpers.mjs';

// Owner boundary: real stdio clients + a separate Hub process. Detects accidental
// per-client state/lifetime ownership; Python tests cannot cover the new transport.
test('MCP clients share identities and messages across client exit and Hub restart', { timeout: 30000 }, async t => {
  const f = await fixture(t);
  let hub = await f.start();
  const alice = await f.connect('alice');
  const bob = await f.connect('bob');
  assert.deepEqual((await call(alice, 'list_peers')).map(a => a.id), ['alice', 'bob']);
  const requestId = randomUUID();
  const message = await call(alice, 'send_message', { recipient_id: 'bob', request_id: requestId, text: 'Review the change' });
  assert.equal(message.senderId, 'alice');
  assert.deepEqual(await call(bob, 'read_inbox'), [message]);
  const wrongAck = await alice.callTool({ name: 'acknowledge_message', arguments: { message_id: message.id } });
  assert.equal(wrongAck.isError, true);
  await alice.close();
  assert.equal((await f.request('/health')).status, 'ok');
  await stop(hub, 'SIGKILL');
  hub = await f.start();
  // Bob's already-running proxy discovers the new port and keeps its identity.
  assert.deepEqual(await call(bob, 'read_inbox'), [message]);
  await call(bob, 'acknowledge_message', { message_id: message.id });
  assert.deepEqual(await call(bob, 'read_inbox'), []);
  const reply = await call(bob, 'send_message', {
    recipient_id: 'alice', request_id: randomUUID(), thread_id: message.threadId, text: 'Review complete',
  });
  await bob.close();
  const resumed = await f.connect('alice');
  assert.deepEqual(await call(resumed, 'read_inbox'), [reply]);
  const events = await call(resumed, 'read_events');
  assert.deepEqual(events.map(e => e.type), [
    'agent.registered', 'agent.registered', 'message.queued', 'message.acknowledged', 'message.queued',
  ]);
});

// Durable retry contract: a lost response must not create a second message/event.
test('message retries and acknowledgements remain idempotent after abrupt Hub death', { timeout: 20000 }, async t => {
  const f = await fixture(t);
  const hub = await f.start();
  await f.request('/v1/agents', agent('a'));
  await f.request('/v1/agents', agent('b'));
  const input = { senderId: 'a', recipientId: 'b', requestId: randomUUID(), text: 'Persist me' };
  const receipt = await f.request('/v1/messages', input);
  await stop(hub, 'SIGKILL');
  await f.start();
  assert.deepEqual(await f.request('/v1/messages', input), receipt);
  await f.request('/v1/messages', { ...input, text: 'Conflicting retry' }, 409);
  assert.deepEqual(await f.request('/v1/inbox?recipientId=b'), [receipt]);
  const ack = await f.request(`/v1/messages/${receipt.id}/ack`, { recipientId: 'b' });
  assert.deepEqual(await f.request(`/v1/messages/${receipt.id}/ack`, { recipientId: 'b' }), ack);
  const events = await f.request('/v1/events?projectId=bridge');
  assert.equal(events.filter(e => e.type === 'message.queued').length, 1);
  assert.equal(events.filter(e => e.type === 'message.acknowledged').length, 1);
});

// SSE adds transport risks beyond SQLite persistence: pagination, replay-to-live
// ordering and reconnect cursors. Exercise real sockets, not EventEmitter mocks.
test('SSE replays multiple pages then delivers live events and resumes by cursor', { timeout: 30000 }, async t => {
  const f = await fixture(t);
  await f.start();
  await f.request('/v1/agents', agent('a'));
  await f.request('/v1/agents', agent('b'));
  await f.request('/v1/agents', agent('unrelated', 'other'));
  const messages = [];
  for (let i = 0; i < 103; i++) messages.push(await f.request('/v1/messages', {
    senderId: 'a', recipientId: 'b', requestId: randomUUID(), text: i < 12 ? '界'.repeat(20000) : `Message ${i}`,
  }));
  const events = await stream(f, 2);
  t.after(() => events.close());
  let sequence = 2;
  for (const message of messages) {
    const event = await events.next();
    assert.ok(event.sequence > sequence);
    assert.equal(event.data.id, message.id);
    sequence = event.sequence;
  }
  await f.request(`/v1/messages/${messages[0].id}/ack`, { recipientId: 'b' });
  const live = await events.next();
  assert.equal(live.type, 'message.acknowledged');
  assert.ok(live.sequence > sequence);
  await events.close();
  const pending = await f.request('/v1/inbox?recipientId=b');
  assert.equal(pending.length, 100);
  assert.equal(pending[0].id, messages[1].id);
  await f.request(`/v1/messages/${messages[1].id}/ack`, { recipientId: 'b' });
  const resumed = await stream(f, live.sequence);
  t.after(() => resumed.close());
  const replay = await resumed.next();
  assert.equal(replay.data.id, messages[1].id);
  assert.ok(replay.sequence > live.sequence);
});

// Process ownership and HTTP input are independent public boundaries.
test('one Hub owns a profile and invalid or unauthenticated requests do not mutate state', { timeout: 20000 }, async t => {
  const f = await fixture(t);
  await f.start();
  const duplicate = spawn(process.execPath, [cli, 'hub', '--home', f.home], { stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  duplicate.stderr.on('data', chunk => { stderr += chunk; });
  const [code] = await once(duplicate, 'exit');
  assert.equal(code, 1);
  assert.match(stderr, /already owns/);
  await f.request('/health', undefined, 401, { Authorization: 'Bearer wrong' });
  await f.request('/health', undefined, 401, { Origin: 'https://unrelated.example' });
  await f.request('/v1/agents', agent('a'));
  await f.request('/v1/agents', agent('b', 'other'));
  await f.request('/v1/agents', { ...agent('a'), projectId: 'other' }, 409);
  await f.request('/v1/messages', {
    senderId: 'a', recipientId: 'b', requestId: randomUUID(), text: 'Wrong project',
  }, 409);
  await f.request('/v1/messages', { senderId: 'a', recipientId: 'b', text: 'Missing retry identity' }, 400);
  await f.request('/v1/agents', agent('c'));
  const large = await f.request('/v1/messages', { senderId: 'a', recipientId: 'c', requestId: randomUUID(), text: 'x'.repeat(140_000) });
  assert.equal((await f.request('/v1/inbox?recipientId=c'))[0].text, large.text);
  assert.deepEqual(await f.request('/v1/inbox?recipientId=b'), []);
  assert.deepEqual((await f.request('/v1/events?projectId=bridge')).map(e => e.type), ['agent.registered', 'agent.registered', 'message.queued']);
});
