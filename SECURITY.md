# Security policy

Report vulnerabilities privately with a GitHub private security advisory for
the Praxis repository. Do not open a public issue containing an exploit,
credential, private workload data, or a bypass of authority, isolation,
approval, verification, effect, replay, or redaction controls.

Include the affected version, minimal reproduction, impact, and any relevant
trust boundary. Replace real credentials and payloads with inert fixtures.
Maintainers should preserve evidence, assess containment, add a regression test,
and coordinate disclosure only after a verified fix is available.

Runtime secrets belong in the host's protected secret mechanism. Praxis source,
specs, events, fixtures, logs, and generated qualification artifacts must not
contain live secret values or recovery material.
