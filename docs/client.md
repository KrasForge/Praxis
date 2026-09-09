# Python v1 client

The client ships in praxis-runtime with no extra dependencies. Configure credentials from a host secret provider; never hard-code them in a spec.

```python
from praxis.client import Client, ClientAPIError
from praxis.client.http import ClientHTTPTransport
from praxis.kernel.spec import ProcessSpec
from praxis.transport.http import TransportError

async def run(base_url, token):
    client = Client(ClientHTTPTransport(base_url, headers={"Authorization": "Bearer " + token}))
    receipt = await client.submit(ProcessSpec("example", "fake"), idempotency_key="example-1")
    cursor = 0
    async for stored in client.events(receipt.process_id, after=cursor):
        cursor = stored.cursor  # persist after the consumer has processed this event
    view = await client.inspect(receipt.process_id)
    return view.result
```

`Submission`, `ProcessView`, and `ControlReceipt` are typed response models. `ProcessView.result` is the core v1 ProcessResult; `events` yields StoredEvent containing the core Event. Additional inspection fields are in `view.data`, including redacted spec, blocking reason, last event, and recovery metadata. Redacted specs are diagnostic views, not guaranteed resubmission documents.

Use `client.control(pid, current_attempt_id, "cancel", policy="tree")` for subtree cancellation. Other operations are suspend, resume, signal (with signal=...), and retry (with retry policy fields). Stale attempts return ClientAPIError(409). Refused or unsupported executor controls remain explicit control data. No operation silently retries a mutation.

TransportError reports network or malformed-wire failure. ClientAPIError reports an HTTP/API rejection. A failed process is ordinary result data with state, outcome, verification, and a structured error; it does not raise a transport exception. On interrupted SSE, reconnect with the last processed cursor. The HTTP implementation has bounded per-frame reads and deadlines, rejects redirects, closes streams on cancellation, and does not automatically resubmit work. Use the same idempotency key to resolve a lost submission acknowledgement.
