# Python v1 client

The client comes in praxis-runtime and it needs no other dependency. Get the
credentials from the secret provider of the host. Never write them in a spec.

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

`Submission`, `ProcessView` and `ControlReceipt` are typed models of a response.
`ProcessView.result` is the core ProcessResult of v1. `events` yields a
StoredEvent that contains the core Event.

The other fields for inspection are in `view.data`. They include the redacted
spec, the reason for a block, the last event and the metadata for recovery. A
redacted spec is a view for diagnosis. Praxis does not promise that you can
submit it again.

Use `client.control(pid, current_attempt_id, "cancel", policy="tree")` to cancel
a subtree. The other operations are suspend, resume, signal (with `signal=...`)
and retry (with the fields of a retry policy). A stale attempt returns
`ClientAPIError(409)`. If an executor refuses a control, or does not support it,
that fact stays explicit in the control data. No operation retries a mutation
silently.

`TransportError` reports a failure of the network or of the wire format.
`ClientAPIError` reports a rejection by HTTP or by the API. A process that failed
is ordinary result data: it has a state, an outcome, a verification and a
structured error, and it raises no transport exception.

If SSE stops, reconnect with the last cursor that you processed. The HTTP
implementation bounds each frame read, applies deadlines, rejects redirects and
closes a stream on cancellation. It never submits work again automatically. To
resolve an acknowledgement of a submission that you lost, use the same
idempotency key.
