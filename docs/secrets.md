# Secret providers

Implement `SecretProvider.resolve(name) -> str` in the trusted host, for example
as a client of a vault. Make a `SecretAccess(provider, authority, redaction)`
with the same authority as the kernel, and the same `RedactionPolicy` as your
exporters for diagnostics.

Give a local executor the access object and the bindings that the host owns, for
example `secret_bindings={"SERVICE_TOKEN": "service/token"}`. Issue
`Resource.SECRET` with the action `read`, scoped to `service/token`, to the
process before you start it. Praxis installs no provider by default.

A binding contains a name, never a value, and a ProcessSpec does not control it.
Praxis authorizes the full batch before it resolves any value. A value enters
only the environment of the subprocess. It is absent from the stored request,
and Praxis redacts the output before it makes an Outcome.

A revocation denies later access. It cannot withdraw a value that Praxis already
gave to a running process. If you must withdraw one, stop that process and
rotate the credential.

Do not put a credential in the environment, the configuration or the inputs.
Those fields are ordinary stored data. Praxis converts an exception from a
provider into a stable error that holds no exception text.

A remote worker and a third-party agent SDK must resolve credentials locally,
through trusted adapters of the host. A value that the controller resolved must
never go into a dispatch envelope.

A workload that holds a secret can encode it deliberately, or write it to an
artifact. Thus you must restrict the publication of artifacts and the access to
the network, as TM-7 describes. To deliver a secret is not a claim of protection
against a recipient that you authorized and that then behaves badly.
