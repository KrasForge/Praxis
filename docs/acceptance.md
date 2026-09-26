# Live provider acceptance

Qualification uses offline fixtures. They qualify how Praxis maps a provider and
its errors. They do not show that a real provider, credential or deployment
works. The acceptance suite in `acceptance/` does that. It is opt-in, and it
never runs on a pull request.

## What it checks

| Check | Needs | Asserts |
| --- | --- | --- |
| Codex: verified success, contract failure, cancellation | `CODEX_API_KEY`, the `codex` CLI | A written file publishes. A missing output publishes nothing. A cancel stops the run. |
| Claude: verified success, contract failure | `ANTHROPIC_API_KEY`, `claude-agent-sdk` | A tool-free answer verifies. A required file fails verification. |
| DeepSeek: verified success, contract failure | `DEEPSEEK_API_KEY`, the DeepSeek harness SDK | As for Codex, without cancellation. The adapter does not advertise it. |
| Noesis: context search | `PRAXIS_ACCEPTANCE_NOESIS_URL`, `_TOKEN`, `_DOMAIN` | A search through `praxis.host` returns `available`. |
| Noesis: allowlist refusal | The same | A domain outside `context_domains` fails locally, and Noesis gets no request. |
| Noesis: publication | Also `_PUBLISH=1` and `_INGEST_PATH` | A scratch publication is `accepted`, and its replay is `duplicate`. |
| Deployed host (the Modulo path) | `PRAXIS_ACCEPTANCE_HOST_URL`, `_TOKEN` | A delegated submit streams events to `process.result`. Inspect shows a terminal state. The idempotent replay returns the same process. |

The agent checks also need `PRAXIS_ACCEPTANCE_ISOLATED_WORKER=1`. Set it only on
a disposable worker that something else confines. The adapters'
`isolated_worker=True` flag does not make a sandbox. See
[isolation](isolation.md).

A check skips when its settings are absent. The record gives the reason.

## Settings

| Variable | Purpose |
| --- | --- |
| `PRAXIS_ACCEPTANCE_CODEX_BIN` | The Codex CLI path. The default is `codex`. |
| `PRAXIS_ACCEPTANCE_{CODEX,CLAUDE,DEEPSEEK}_MODEL` | An optional model for each adapter. |
| `PRAXIS_ACCEPTANCE_CANCEL_AFTER` | The seconds before the Codex cancel. The default is 5. |
| `PRAXIS_ACCEPTANCE_NOESIS_QUERY` | The search text. The default is `praxis`. |
| `PRAXIS_ACCEPTANCE_NOESIS_{CA,CLIENT_CERT,CLIENT_KEY}` | PEM files for a private CA and mutual TLS to Noesis. |
| `PRAXIS_ACCEPTANCE_HOST_{CA,CLIENT_CERT,CLIENT_KEY}` | PEM files for mutual TLS to the host. |
| `PRAXIS_ACCEPTANCE_HOST_USER` | The delegated user. The default is `praxis-acceptance`. |
| `PRAXIS_ACCEPTANCE_HOST_EXECUTOR` | The executor to submit to. The default is `fake`. |
| `PRAXIS_ACCEPTANCE_RECORD` | The record path. The default is `acceptance-record.json`. |

The host token must belong to a client with `delegate = true` and the `submit`
and `read` roles. The Codex key reaches the CLI through a secret binding, not
through `ProcessSpec.environment`. See [secrets](secrets.md).

## Run it

On a confined worker:

```sh
export PRAXIS_ACCEPTANCE_ISOLATED_WORKER=1 CODEX_API_KEY=... ANTHROPIC_API_KEY=...
uv run pytest acceptance -rs
```

In GitHub, run the **Acceptance** workflow manually. It also runs every Monday.
It uses the `acceptance` environment:

1. Protect the environment with required reviewers.
2. Put the credentials in its secrets. PEM material goes in secrets that end in
   `_PEM`.
3. Put the endpoints and switches in its variables.
4. Pin the provider clients in the variables `PRAXIS_ACCEPTANCE_NPM` and
   `PRAXIS_ACCEPTANCE_PIP`.

The record is uploaded as the artifact `acceptance-record`.

## The record and the secret canary

Each run writes a JSON record with these contents:

- The Praxis and Python versions.
- The provider versions that the checks saw.
- The outcome of each check, and why it skipped or failed.

Before the record is written, the suite searches these places for each secret
value it knows: the record, and every runtime database, workspace and canonical
directory that a check created. A match fails the run, and the record lists
where the value was found. The record never includes the value.

`tests/test_acceptance_harness.py` runs the suite in CI against local stand-ins.
It proves three things:

- Every check skips when no credentials are set.
- Every check passes against providers that behave correctly.
- The canary fails a run in which an agent writes its credential into an
  artifact.

The harness says nothing about the real providers.

## Records

Commit a successful record as `docs/acceptance/<date>.json`.
Then narrow the first deployment limit in [qualification](qualification.md) to
what the record does not cover.

| Date | Praxis | Checks passed | Record |
| --- | --- | --- | --- |
| — | — | No live run is recorded yet | — |
