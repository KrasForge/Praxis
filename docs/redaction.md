# Observability redaction

HTTP inspection, errors and SSE events go through `RedactionPolicy` before the
bytes leave the controller. Praxis decodes a nested JSON document and redacts it
too. By default it hides the fields for environment, credential, secret,
password, API key, authorization and capability.

A host registers its true secret values with the policy. Praxis then removes
them from free text, including the output of an executor. Praxis cannot identify
an arbitrary secret that carries no label.

Use the `emit` method of the same policy for your custom exporters of events and
traces. Put `RedactingLogFilter` on every logging handler. The filter removes
the raw text of an exception and of a stack. Use the structured runtime errors
for diagnosis instead. Praxis installs no logging handler and no external
exporter implicitly.

The internal event journal is a privileged database for recovery. It is not an
export for observability. It keeps the specifications and the capability records
that a replay needs. Restrict the access to the database and to its backups.

Never put a secret value in a specification. A reference to a secret provider is
the boundary for integration. Redaction copies the data; it does not change the
records for recovery. A structured runtime error record holds a stable code and
the causal records. It never holds the raw text of an exception.
