import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DatabaseSync } from 'node:sqlite';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store } from '../dist/store.js';

test('v1 message retries retain their receipt and acknowledgement after database migration', async t => {
  const home = await mkdtemp(join(tmpdir(), 'bridge-migration-'));
  t.after(() => rm(home, { recursive: true, force: true }));
  const path = join(home, 'hub.sqlite'), old = new DatabaseSync(path);
  // Frozen v1 storage contract: five-field request key, no wake field.
  old.exec(`CREATE TABLE agents(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE messages(id TEXT PRIMARY KEY, sender_id TEXT, recipient_id TEXT, request_id TEXT, request TEXT, acknowledged INTEGER DEFAULT 0, data TEXT);
    CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, data TEXT);
    PRAGMA user_version=1;`);
  for (const id of ['a', 'b']) old.prepare('INSERT INTO agents VALUES (?, ?, ?)').run(id, 'p', JSON.stringify({ id, projectId: 'p', kind: 'external', name: id }));
  const input = { senderId: 'a', recipientId: 'b', requestId: 'retry', text: 'Persisted before upgrade' };
  const receipt = { ...input, id: 'old-message', projectId: 'p', threadId: 'thread', createdAt: '2026-01-01', acknowledgedAt: '2026-01-02' };
  old.prepare('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)').run(receipt.id, 'a', 'b', 'retry', JSON.stringify(['a', 'b', 'retry', null, input.text]), 1, JSON.stringify(receipt));
  old.close();
  const store = new Store(path);
  try {
    const retried = store.send({ ...input, wake: true, userRequested: false });
    assert.equal(retried.id, receipt.id); assert.equal(retried.acknowledgedAt, receipt.acknowledgedAt);
    assert.deepEqual(store.inbox('b'), []); assert.deepEqual(store.events('p', 0), []);
    assert.throws(() => store.send({ ...input, wake: false, userRequested: false }), /different message/);
  } finally { store.close(); }
});

test('task dispatch persists only session fields and a late native update cannot reopen a closed session', async t => {
  const store = new Store(':memory:');
  try {
    store.register({ id: 'a', kind: 'external', projectId: 'p', name: 'a' });
    const task = store.dispatch({ agent: 'kimi', projectId: 'p', cwd: '/project', requesterId: 'a', message: 'Task-only prompt', userRequested: false });
    const active = store.session(task.sessionId);
    await t.test('session projection', () => assert.equal('message' in active, false, 'task prompt must not leak into session records/events'));
    const closing = store.session(task.sessionId); closing.state = 'closed'; store.saveSession(closing);
    active.nativeSessionId = 'late-native-id'; active.state = 'running'; store.saveSession(active);
    await t.test('closed is terminal', () => assert.equal(store.session(task.sessionId).state, 'closed'));
  } finally { store.close(); }
});
