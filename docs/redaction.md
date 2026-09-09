# Observability redaction

HTTP inspection, errors, and SSE events pass through `RedactionPolicy` before bytes leave the controller. Nested JSON documents are decoded and redacted too. Environment, credential, secret, password, API-key, authorization, and capability fields are hidden by default. Hosts register actual secret values with the policy to remove them from free text (including executor output). Arbitrary unlabelled secrets cannot be identified automatically.

Use the same policy's `emit` for custom event/trace exporters and `RedactingLogFilter` on every logging handler. The filter removes raw exception and stack text; use structured runtime errors for diagnostics. No logging handlers or external exporters are installed implicitly.

The internal event journal is a privileged recovery database, not an observability export. It retains specifications and capability records needed for replay. Restrict database and backup access. Never put secret values in a specification; secret-provider references are the integration boundary. Redaction copies data and does not mutate recovery records. Structured runtime error records contain stable codes and causal records, never raw exception messages.
