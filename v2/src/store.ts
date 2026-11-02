import { randomBytes, randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { HubError } from './contracts.js';
import type { Agent, Message, HubEvent, RegisterAgent, SendMessage, Session, Task, CreateSession, DispatchTask } from './contracts.js';

// SQLite owns state. Notifications are hints; all consumers replay from this log.
export class Store {
  readonly #db: DatabaseSync;

  constructor(path: string) {
    this.#db = new DatabaseSync(path);
    try {
      this.#db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;');
      const version = Number(this.#db.prepare('PRAGMA user_version').get()!['user_version']);
      if (version > 3) throw new Error('This database requires a newer Agent Bridge Hub');
      if (version === 0) this.#db.exec(`
        BEGIN IMMEDIATE;
        CREATE TABLE agents (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL) STRICT;
        CREATE TABLE messages (
          id TEXT PRIMARY KEY,
          sender_id TEXT NOT NULL REFERENCES agents(id),
          recipient_id TEXT NOT NULL REFERENCES agents(id),
          request_id TEXT NOT NULL,
          request TEXT NOT NULL,
          acknowledged INTEGER NOT NULL DEFAULT 0,
          data TEXT NOT NULL,
          UNIQUE(sender_id, request_id)
        ) STRICT;
        CREATE INDEX inbox ON messages(recipient_id, acknowledged);
        CREATE TABLE events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id TEXT NOT NULL,
          data TEXT NOT NULL
        ) STRICT;
        CREATE INDEX project_events ON events(project_id, sequence);
        PRAGMA user_version=1;
        COMMIT;
      `);
      if (version < 2) this.#db.exec(`
        BEGIN IMMEDIATE;
        CREATE TABLE credentials (agent_id TEXT PRIMARY KEY REFERENCES agents(id), token TEXT UNIQUE NOT NULL) STRICT;
        CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT UNIQUE NOT NULL REFERENCES agents(id), project_id TEXT NOT NULL, data TEXT NOT NULL) STRICT;
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
          project_id TEXT NOT NULL, requester_id TEXT NOT NULL REFERENCES agents(id),
          request_id TEXT, source_message_id TEXT UNIQUE REFERENCES messages(id),
          status TEXT NOT NULL, request TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', data TEXT NOT NULL,
          UNIQUE(requester_id, request_id)
        ) STRICT;
        CREATE INDEX session_queue ON tasks(session_id, status);
        CREATE TABLE preferences (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL) STRICT;
        PRAGMA user_version=2;
        COMMIT;
      `);
      if (version < 3) this.#db.exec(`
        BEGIN IMMEDIATE;
        UPDATE messages SET request=json_insert(request, '$[#]', json('true')),
          data=json_set(data, '$.wake', json('true')) WHERE json_array_length(request)=5;
        PRAGMA user_version=3;
        COMMIT;
      `);
    } catch (error) {
      this.#db.close();
      throw error;
    }
  }

  close(): void { this.#db.close(); }

  #transaction<T>(work: () => T): T {
    if (this.#db.isTransaction) return work();
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      this.#db.exec('COMMIT');
      return result;
    } catch (error) {
      this.#db.exec('ROLLBACK');
      throw error;
    }
  }

  #event(type: HubEvent['type'], projectId: string, data: unknown): void {
    this.#db.prepare('INSERT INTO events(project_id, data) VALUES (?, ?)').run(
      projectId, JSON.stringify({ projectId, type, data, createdAt: new Date().toISOString() }),
    );
  }

  agent(agentId: string): Agent {
    const row = this.#db.prepare('SELECT data FROM agents WHERE id=?').get(agentId);
    if (!row) throw new HubError(404, 'Agent not found');
    return JSON.parse(String(row['data'])) as Agent;
  }

  register(input: RegisterAgent): Agent {
    return this.#transaction(() => {
      const row = this.#db.prepare('SELECT data FROM agents WHERE id=?').get(input.id);
      if (row) {
        const agent = JSON.parse(String(row['data'])) as Agent;
        if (agent.projectId !== input.projectId || agent.kind !== input.kind || agent.name !== input.name) {
          throw new HubError(409, 'Agent ID already registered with different metadata');
        }
        return agent;
      }
      const agent = { ...input, createdAt: new Date().toISOString() };
      this.#db.prepare('INSERT INTO agents VALUES (?, ?, ?)').run(agent.id, agent.projectId, JSON.stringify(agent));
      this.#event('agent.registered', agent.projectId, agent);
      return agent;
    });
  }

  agents(projectId: string): Agent[] {
    return this.#db.prepare('SELECT data FROM agents WHERE project_id=? ORDER BY id').all(projectId)
      .map(row => JSON.parse(String(row['data'])) as Agent);
  }

  send(input: SendMessage): Message {
    // Explicit order makes retry comparison independent of JSON property order.
    const request = JSON.stringify([input.senderId, input.recipientId, input.requestId, input.threadId ?? null, input.text, input.wake]);
    return this.#transaction(() => {
      const sender = this.agent(input.senderId);
      const recipient = this.agent(input.recipientId);
      if (sender.projectId !== recipient.projectId) throw new HubError(409, 'Agents belong to different projects');
      const existing = this.#db.prepare('SELECT request, data FROM messages WHERE sender_id=? AND request_id=?')
        .get(input.senderId, input.requestId);
      if (existing) {
        if (existing['request'] !== request) throw new HubError(409, 'requestId already used for a different message');
        return JSON.parse(String(existing['data'])) as Message;
      }
      const message: Message = {
        ...input, id: randomUUID(), projectId: sender.projectId,
        threadId: input.threadId ?? randomUUID(), createdAt: new Date().toISOString(), acknowledgedAt: null,
      };
      this.#db.prepare(`INSERT INTO messages(id, sender_id, recipient_id, request_id, request, data)
        VALUES (?, ?, ?, ?, ?, ?)`).run(message.id, message.senderId, message.recipientId,
        message.requestId, request, JSON.stringify(message));
      this.#event('message.queued', message.projectId, message);
      const target = this.sessionForAgent(message.recipientId);
      if (target && message.wake) {
        if (target.state === 'closed') throw new HubError(409, 'Recipient session is closed');
        this.#insertTask(target, {
          requesterId: input.senderId, message: input.text,
          requestId: `message:${message.id}`, model: undefined, effort: undefined,
        }, JSON.stringify(['message', message.id]), message.id);
      }
      return message;
    });
  }

  inbox(recipientId: string, limit = 100): Message[] {
    this.agent(recipientId);
    // Acknowledgements move the window forward without an unstable offset.
    return this.#db.prepare('SELECT data FROM messages WHERE recipient_id=? AND acknowledged=0 ORDER BY rowid LIMIT ?')
      .all(recipientId, limit).map(row => JSON.parse(String(row['data'])) as Message);
  }

  acknowledge(messageId: string, recipientId: string): Message {
    return this.#transaction(() => {
      const row = this.#db.prepare('SELECT data FROM messages WHERE id=?').get(messageId);
      if (!row) throw new HubError(404, 'Message not found');
      const message = JSON.parse(String(row['data'])) as Message;
      if (message.recipientId !== recipientId) throw new HubError(409, 'Only the recipient can acknowledge this message');
      if (message.acknowledgedAt !== null) return message;
      message.acknowledgedAt = new Date().toISOString();
      this.#db.prepare('UPDATE messages SET acknowledged=1, data=? WHERE id=?').run(JSON.stringify(message), message.id);
      this.#event('message.acknowledged', message.projectId, message);
      return message;
    });
  }

  credential(agentId: string): string {
    this.agent(agentId);
    const found = this.#db.prepare('SELECT token FROM credentials WHERE agent_id=?').get(agentId);
    if (found) return String(found['token']);
    const token = randomBytes(32).toString('hex');
    this.#db.prepare('INSERT INTO credentials VALUES (?, ?)').run(agentId, token);
    return token;
  }

  authenticate(token: string): Agent | undefined {
    const row = this.#db.prepare('SELECT agent_id FROM credentials WHERE token=?').get(token);
    return row ? this.agent(String(row['agent_id'])) : undefined;
  }

  message(messageId: string): Message {
    const row = this.#db.prepare('SELECT data FROM messages WHERE id=?').get(messageId);
    if (!row) throw new HubError(404, 'Message not found');
    return JSON.parse(String(row['data'])) as Message;
  }

  session(sessionId: string): Session {
    const row = this.#db.prepare('SELECT data FROM sessions WHERE id=?').get(sessionId);
    if (!row) throw new HubError(404, 'Session not found');
    return JSON.parse(String(row['data'])) as Session;
  }

  sessionForAgent(agentId: string): Session | undefined {
    const row = this.#db.prepare('SELECT data FROM sessions WHERE agent_id=?').get(agentId);
    return row ? JSON.parse(String(row['data'])) as Session : undefined;
  }

  sessions(projectId?: string): Session[] {
    const rows = projectId === undefined ? this.#db.prepare('SELECT data FROM sessions ORDER BY rowid').all()
      : this.#db.prepare('SELECT data FROM sessions WHERE project_id=? ORDER BY rowid').all(projectId);
    return rows.map(row => JSON.parse(String(row['data'])) as Session);
  }

  createSession(input: CreateSession): Session {
    return this.#transaction(() => {
      const sessionId = randomUUID();
      const session: Session = {
        agent: input.agent, projectId: input.projectId, cwd: input.cwd,
        id: sessionId, agentId: `session-${sessionId}`, nativeSessionId: null,
        model: input.model ?? null, effort: input.effort ?? null, title: input.title ?? input.agent,
        state: 'unloaded', createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
      };
      this.register({ id: session.agentId, kind: input.agent, projectId: input.projectId, name: session.title });
      this.#db.prepare('INSERT INTO sessions VALUES (?, ?, ?, ?)').run(session.id, session.agentId, session.projectId, JSON.stringify(session));
      this.#event('session.created', session.projectId, session);
      return session;
    });
  }

  saveSession(session: Session): void {
    // A late adapter update can still carry the pre-close state.
    if (session.state !== 'closed' && this.session(session.id).state === 'closed') session.state = 'closed';
    session.updatedAt = new Date().toISOString();
    this.#db.prepare('UPDATE sessions SET data=? WHERE id=?').run(JSON.stringify(session), session.id);
  }

  #insertTask(session: Session, input: Pick<DispatchTask, 'requesterId' | 'requestId' | 'message' | 'model' | 'effort'>,
    request: string, sourceMessageId: string | null = null): Task {
    const task: Task = {
      id: randomUUID(), sessionId: session.id, agent: session.agent, projectId: session.projectId,
      requesterId: input.requesterId, requestId: input.requestId ?? null, sourceMessageId,
      message: input.message, model: input.model ?? null, effort: input.effort ?? null,
      status: 'queued', createdAt: new Date().toISOString(), startedAt: null, finishedAt: null,
      text: '', stopReason: '', error: null, warnings: [], observedModel: null, observedEffort: null,
      usage: {}, filesChanged: [], filesOutsideCwd: [], filesChangedState: 'pending',
    };
    const { text: _text, ...data } = task;
    this.#db.prepare(`INSERT INTO tasks(id, session_id, project_id, requester_id, request_id, source_message_id, status, request, data)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(task.id, task.sessionId, task.projectId, task.requesterId,
      task.requestId, task.sourceMessageId, task.status, request, JSON.stringify(data));
    this.#event('task.queued', task.projectId, { taskId: task.id, sessionId: task.sessionId, requesterId: task.requesterId });
    return task;
  }

  dispatch(input: DispatchTask): Task {
    return this.#transaction(() => {
      const request = JSON.stringify(Object.entries(input).sort(([a], [b]) => a.localeCompare(b)));
      if (input.requestId) {
        const existing = this.#db.prepare('SELECT id, request FROM tasks WHERE requester_id=? AND request_id=?')
          .get(input.requesterId, input.requestId);
        if (existing) {
          if (existing['request'] !== request) throw new HubError(409, 'requestId already used with different task arguments');
          return this.task(String(existing['id']));
        }
      }
      const session = input.sessionId ? this.session(input.sessionId) : this.createSession(input);
      if (session.projectId !== input.projectId || session.agent !== input.agent || session.cwd !== input.cwd) {
        throw new HubError(409, 'Session agent, project and cwd must match');
      }
      if (session.state === 'closed') throw new HubError(409, 'Session is closed');
      return this.#insertTask(session, input, request);
    });
  }

  task(taskId: string): Task {
    const row = this.#db.prepare('SELECT data, output FROM tasks WHERE id=?').get(taskId);
    if (!row) throw new HubError(404, 'Task not found');
    return { ...JSON.parse(String(row['data'])), text: String(row['output']) } as Task;
  }

  tasks(projectId: string, sessionId?: string): Task[] {
    const rows = sessionId === undefined
      ? this.#db.prepare('SELECT id FROM tasks WHERE project_id=? ORDER BY rowid').all(projectId)
      : this.#db.prepare('SELECT id FROM tasks WHERE project_id=? AND session_id=? ORDER BY rowid').all(projectId, sessionId);
    return rows.map(row => this.task(String(row['id'])));
  }

  nextTask(sessionId: string): Task | undefined {
    const row = this.#db.prepare("SELECT id FROM tasks WHERE session_id=? AND status='queued' ORDER BY rowid LIMIT 1").get(sessionId);
    return row ? this.task(String(row['id'])) : undefined;
  }

  saveTask(task: Task, event = true): void {
    this.#transaction(() => {
      const { text, ...data } = task;
      this.#db.prepare('UPDATE tasks SET status=?, output=?, data=? WHERE id=?').run(task.status, text, JSON.stringify(data), task.id);
      if (event) this.#event(`task.${task.status}`, task.projectId, {
        taskId: task.id, sessionId: task.sessionId, requesterId: task.requesterId, status: task.status, error: task.error,
      });
    });
  }

  appendOutput(task: Task, text: string): void {
    this.#transaction(() => {
      this.#db.prepare('UPDATE tasks SET output=output||? WHERE id=?').run(text, task.id);
      this.#event('task.output', task.projectId, { taskId: task.id, sessionId: task.sessionId, text });
    });
  }

  record(projectId: string, type: string, data: unknown): void { this.#event(type, projectId, data); }

  recover(): void {
    this.#transaction(() => {
      for (const row of this.#db.prepare("SELECT id FROM tasks WHERE status IN ('queued', 'running')").all()) {
        const task = this.task(String(row['id']));
        task.error = `Hub restarted while task was ${task.status}; inspect the workspace before explicitly retrying`;
        task.status = 'interrupted'; task.finishedAt = new Date().toISOString();
        this.saveTask(task);
      }
      for (const session of this.sessions()) if (session.state !== 'closed') {
        session.state = 'unloaded'; this.saveSession(session);
      }
    });
  }

  preferences(): { mode: string; instructions: string } | undefined {
    const row = this.#db.prepare('SELECT data FROM preferences WHERE id=1').get();
    return row ? JSON.parse(String(row['data'])) as { mode: string; instructions: string } : undefined;
  }

  setPreferences(value: { mode: string; instructions: string }): void {
    this.#db.prepare('INSERT INTO preferences VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data').run(JSON.stringify(value));
  }

  events(projectId: string, after: number, limit = 100): HubEvent[] {
    return this.#db.prepare('SELECT sequence, data FROM events WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?')
      .all(projectId, after, limit).map(row => ({ ...JSON.parse(String(row['data'])), sequence: Number(row['sequence']) }) as HubEvent);
  }
}
