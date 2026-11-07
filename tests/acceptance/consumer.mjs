/** A separate ESM application importing the installed npm package. */
import assert from 'node:assert/strict';
import {Pliqra} from '@eturkok/pliqra';
const client = new Pliqra(process.env.PLIQRA_URL, process.env.PLIQRA_API_KEY);
const service = process.env.PLIQRA_SERVICE;
const available = await client.services();
assert(available.some(item => item.name === service));
const bound = await client.connect(service, {requirements: ['chat', 'streaming']});
let text = '';
for await (const part of bound.stream([{role: 'user', content: 'Synthetic installed Node application'}], {max_tokens: 64})) {
  for (const choice of part.choices || []) text += choice.delta?.content || '';
}
assert(text.includes('Synthetic customer acceptance') && text.includes('🌍'));
console.log(JSON.stringify({application: 'installed-npm-sdk', streaming: 'passed', gatewayImported: false, synthetic: true}));
