# Clients and protocol conformance

The protocol remains **pliqra/0.1-draft**. The independently executable profile is **pliqra-core/1**. Product version 2.1 does not imply a standardized or independently certified protocol.

| Client | Files | Capabilities |
|---|---|---|
| Python, standard library | `sdk/pliqra_client.py` | Discovery, bind, complete/stream, operations, batches |
| JavaScript, Node 22+ / modern browser | `sdk/javascript/pliqra.mjs` | Fetch transport, aborts, Unicode SSE, extension/409 handling, operations, batches |
| TypeScript | `sdk/javascript/pliqra.d.mts` | Types for the same JavaScript client |
| Go 1.23+ | `sdk/go` | Context-aware discovery, bindings, completions, streaming and model operations |

[Install Python/JavaScript packages →](SDK.md)

Files are also downloadable in **Settings & API**. All clients reject credential-forwarding redirects and non-local invocation paths, enforce response bounds and detect incomplete streams. A changed contract raises a specific error; applications decide when to rediscover/rebind, rather than silently accepting new policy.

```python
from pliqra_client import Pliqra
client = Pliqra('http://localhost:8088', 'YOUR_SCOPED_KEY')
connection = client.connect('support', ['chat'], required_extensions=['pliqra.prompts/1'])
print(connection.complete([{'role':'user','content':'Hello'}], max_tokens=128))
```

```js
import {Pliqra} from '@eturkok/pliqra';
const client = new Pliqra('http://localhost:8088', process.env.PLIQRA_API_KEY);
const connection = await client.connect('support', {requirements:['chat']});
for await (const part of connection.stream([{role:'user',content:'Hello'}])) {
  process.stdout.write(part.choices?.[0]?.delta?.content || '');
}
```

Organization base URLs include `/org/{slug}`. Keys and bindings remain scoped to that workspace. See [protocol](PROTOCOL.md) for extension identifiers and wire shapes.

## Run the profile

```sh
export PLIQRA_API_KEY='YOUR_SCOPED_KEY'
pliqra-conformance --url http://localhost:8088 --service support
# Adds completion + streaming calls; these can incur provider charges:
pliqra-conformance --url http://localhost:8088 --service support --invoke
```

Seven metadata/negative checks cover discovery, service contracts, explicit binding, unknown capabilities, required/optional extensions and invalid credentials. `--invoke` adds completion and terminal-stream checks. JSON output includes individual pass/fail results, scope and inference flag; exit status fails when any check fails. The suite imports no gateway code. Automated live HTTP tests run it against Pliqra and the separate standard-library reference server with synthetic upstreams. A passing core profile does not certify quality, policy effectiveness, resilience, every optional extension or external adoption.

Run `node sdk/javascript/test.mjs`, `cd sdk/go && go test ./...`, and `python -m pytest tests/test_live_conformance.py -q`. CI runs these checks too. Third parties can use the suite against an independently implemented server; no external attestation has been obtained.
