"""A separate application importing the INSTALLED Python distribution only."""
import json
import os
from pliqra_client import Pliqra, PliqraError
from pliqra_conformance import run

client = Pliqra(os.environ['PLIQRA_URL'], os.environ['PLIQRA_API_KEY'])
service = os.environ['PLIQRA_SERVICE']
report = run(client, service, invoke=True)
assert report['passed'], report
connection = client.connect(service, ['chat', 'streaming'])
text = ''.join(c.get('delta', {}).get('content', '') for packet in connection.stream([{'role': 'user', 'content': 'Synthetic installed application'}], max_tokens=64) for c in packet.get('choices', []))
assert 'Synthetic customer acceptance' in text and '🌍' in text
print(json.dumps({'application': 'installed-python-sdk', 'core_checks': len(report['results']), 'streaming': 'passed', 'gateway_imported': False, 'synthetic': True}))
