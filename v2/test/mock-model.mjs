import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';

export const prompt = spec => `BRIDGE_SMOKE=${JSON.stringify(spec)}`;
const text = content => typeof content === 'string' ? content : (content ?? []).map(x => x.text ?? '').join('\n');
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;

// Only the model is simulated. Tool execution, ACP, MCP and persistence belong
// to the installed product and Bridge processes, never to this server.
export async function mockModel() {
  const requests = [], gates = new Map(), errors = [];
  function gate(name) {
    if (!gates.has(name)) gates.set(name, Promise.withResolvers());
    return gates.get(name);
  }
  const server = createServer(async (req, res) => {
    try {
      assert.equal(req.url, '/v1/chat/completions');
      let input = '';
      for await (const chunk of req) input += chunk;
      const body = JSON.parse(input);
      const messages = body.messages;
      const start = messages.findLastIndex(m => m.role === 'user' && text(m.content).includes('BRIDGE_SMOKE='));
      // OpenCode may make auxiliary title-generation requests.
      const match = start < 0 ? null : /^BRIDGE_SMOKE=(\{.*\})$/m.exec(text(messages[start].content));
      const spec = match ? JSON.parse(match[1]) : null;
      const results = messages.slice(start + 1).filter(m => m.role === 'tool');
      const record = { id: spec?.id ?? 'auxiliary', model: body.model, tools: (body.tools ?? []).map(x => x.function.name),
        history: messages.filter(m => m.role === 'user').map(m => /^BRIDGE_SMOKE=(\{.*\})$/m.exec(text(m.content)))
          .filter(Boolean).map(m => JSON.parse(m[1]).id), step: results.length, closed: false };
      requests.push(record);
      res.once('close', () => { record.closed = true; });
      const tool = (suffix, args) => {
        const name = record.tools.find(name => name === suffix || name.endsWith(`_${suffix}`));
        assert.ok(name, `Missing tool ${suffix}; offered ${record.tools.join(', ')}`);
        return { id: `call_${randomUUID()}`, type: 'function', function: { name, arguments: JSON.stringify(args) } };
      };
      if (spec?.gate && body.tools?.length && results.length === 0) {
        await Promise.race([gate(spec.gate).promise, once(res, 'close')]);
        if (res.destroyed) return;
      }
      let call;
      if (spec?.file && results.length === 0 && body.tools?.length) {
        let code = `require('node:fs').appendFileSync(${JSON.stringify(spec.file)},${JSON.stringify(spec.line + '\n')}); console.log('write complete')`;
        if (spec.heartbeat) {
          code += `; setInterval(()=>require('node:fs').appendFileSync(${JSON.stringify(spec.heartbeat)},'.'),50)`;
        }
        call = tool('bash', { command: `${quote(process.execPath)} -e ${quote(code)}`, description: 'Write the smoke-test file' });
      }
      if (spec?.delegate && body.tools?.length) {
        const result = index => {
          const raw = text(results[index].content);
          const value = JSON.parse(raw);
          return value.content ? JSON.parse(value.content[0].text) : value;
        };
        if (results.length === 0) call = tool('list_agents', {});
        if (results.length === 1) call = tool('dispatch_task', {
          agent: 'opencode', cwd: spec.cwd, model: 'mock/beta',
          message: prompt({ id: 'child', file: spec.childFile, line: 'child wrote this' }),
        });
        if (results.length === 2) call = tool('wait_task', { task_id: result(1).task_id, timeout_sec: 60 });
        if (results.length === 3) call = tool('get_result', { task_id: result(1).task_id });
        if (results.length === 4) {
          assert.equal(result(3).status, 'completed', JSON.stringify(result(3)));
          const incoming = /Message from ([^;]+); thread ([^.]+)\./.exec(text(messages[start].content));
          assert.ok(incoming, 'Missing source identity/thread');
          call = tool('send_message', { recipient_id: incoming[1], thread_id: incoming[2], request_id: spec.id,
            text: 'real CLI child completed', wake: false });
        }
      }
      const content = spec && body.tools?.length ? `MOCK_DONE:${spec.id}` : 'Mock smoke session';
      record.call = call?.function.name;
      if (!body.stream) {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ id: randomUUID(), object: 'chat.completion', model: body.model,
          choices: [{ index: 0, message: { role: 'assistant', content, ...(call ? { tool_calls: [call] } : {}) }, finish_reason: call ? 'tool_calls' : 'stop' }],
          usage: { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110 } }));
        return;
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const id = randomUUID();
      const chunk = (delta, finish_reason = null) => res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created: 0, model: body.model, choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
      chunk({ role: 'assistant', ...(call ? { tool_calls: [{ index: 0, ...call }] } : { content }) });
      chunk({}, call ? 'tool_calls' : 'stop');
      res.end('data: [DONE]\n\n');
    } catch (error) {
      errors.push(String(error));
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: String(error), type: 'mock_failure' } }));
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return {
    url: `http://127.0.0.1:${server.address().port}/v1`, requests, errors,
    release(name) { gate(name).resolve(); },
    async close() {
      for (const value of gates.values()) value.resolve();
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    },
  };
}
