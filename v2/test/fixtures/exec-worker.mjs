import { appendFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
const args = process.argv.slice(2);
const codex = args[0] === 'exec';
let prompt = '';
for await (const chunk of process.stdin) prompt += chunk;
if (!codex) prompt = JSON.parse(prompt).message.content;
const resume = args.indexOf(codex ? 'resume' : '--conversation');
const id = resume >= 0 ? args[resume + 1] : `${codex ? 'thread' : 'conversation'}-fixture`;
const emit = value => console.log(JSON.stringify(value));
await writeFile(join(process.cwd(), 'exec-argv.json'), JSON.stringify(args));
if (prompt.includes('NESTED_ERROR')) {
  emit({ event: 'result', status: 'ERROR', error: 'nested failure', result: { response: 'Partial result' } });
} else if (prompt.includes('OBJECT_ERROR')) {
  emit({ event: 'result', error: { message: 'structured failure' }, response: 'Partial result' });
} else if (prompt.includes('FAIL_TURN')) {
  emit(codex ? { type: 'turn.failed', error: { message: 'deliberate worker failure' } } : { event: 'result', status: 'ERROR', error: 'deliberate worker failure' });
  process.exitCode = 1;
} else {
  await appendFile(join(process.cwd(), 'exec-output.txt'), id + '\n');
  if (codex) {
    emit({ type: 'thread.started', thread_id: id });
    emit({ type: 'error', message: 'transient retry', will_retry: true });
    emit({ type: 'item.completed', item: { type: 'agent_message', text: JSON.stringify({ id, resumed: resume >= 0 }) } });
    emit({ type: 'item.completed', item: { type: 'file_change', changes: [{ path: 'exec-output.txt' }] } });
    emit({ type: 'turn.completed', usage: { input_tokens: 20, output_tokens: 10 } });
  } else {
    emit({ event: 'init', conversation_id: id });
    emit({ event: 'step', step_update: { step_type: 'agent_response', text_delta: 'partial' } });
    emit({ event: 'result', status: 'SUCCESS', conversation_id: id, response: JSON.stringify({ id, resumed: resume >= 0 }), usage: { tokens: 30 } });
  }
}
