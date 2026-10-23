# Plaid webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. The agent never receives
the Plaid client id or secret: the person writes the secret files.

## Needs from the human

- The Plaid client id and secret for the environment in use, written by the person into
  `/etc/hookspool/secrets/plaid_client_id` and `/etc/hookspool/secrets/plaid_secret` (step 2). The agent does
  not read, print or repeat them.
- The environment (`production`, `development` or `sandbox`), the public HTTPS webhook URL (`<WEBHOOK_URL>`)
  and the Hermes route for the events (`<ROUTE>`).
- Access to the Plaid Dashboard and the Plaid API calls that set webhook URLs, performed by the person (step 6).
- Approval before any webhook URL is changed at Plaid (step 6) and before it is changed back (step 8).

Secrets used below: `client_id`, `secret`. Setting: `env` (not required; default `production`).

## Steps

1. **Goal:** the `plaid` extra (PyJWT and cryptography, which check the ES256 signature) is installed in the
   Python environment hookspool runs from: `pip install 'hookspool[plaid]'` (or the same extra of your install
   method). Without it every webhook answers 503 and `hookspool check` does not notice.
   **Check:** `python -c "import jwt, cryptography"` (with that environment's `python`) exits 0.
   **If it fails:** install the extra, then repeat the check.
2. **Goal:** the secret files exist. The person writes the client id and the secret to the two files with mode
   0600. The agent only checks them.
   **Check:** `ls -l /etc/hookspool/secrets/plaid_client_id /etc/hookspool/secrets/plaid_secret` shows mode
   `-rw-------` and a non-zero size. **If it fails:** ask the person to write the files again.
3. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.plaid]
   secrets.client_id = { file = "/etc/hookspool/secrets/plaid_client_id" }
   secrets.secret = { file = "/etc/hookspool/secrets/plaid_secret" }
   settings = { env = "<ENV>" }
   outputs = { "*" = "hermes:<ROUTE>" }
   ```
   **Check:** `hookspool bobbin expand plaid` succeeds and shows the secrets as references only.
   **If it fails:** read the error; it names the missing secret or output. (`expand` does not judge the `env`
   value; the next step does.)
4. **Goal:** the config is valid and loaded. Run `hookspool check`, then `hookspool reload`.
   **Check:** no error mentions `plaid`. **If it fails:** an `env: must be one of ...` error means a value
   other than the three environments; fix the table. Fix any other error in the table too, do not change the
   bobbin.
5. **Goal:** the reverse proxy passes the request on unchanged. Confirm `/hooks/plaid` reaches the intake with
   its body and the `Plaid-Verification` header, and carries a per-client rate limit.
   **Check:** `hookspool status` is ok and the proxy config lists the path. **If it fails:** fix the proxy.
6. **Outward, human-performed:** the person sets the webhook URL `<WEBHOOK_URL>` at Plaid (the `webhook` field of
   the Link token, or `/item/webhook/update` for an existing item). In Sandbox the person can fire a test with
   `/sandbox/item/fire_webhook`. The agent does not call Plaid and does not see the credentials. Ask the person to
   confirm when it is done.
7. **Goal:** a real webhook is accepted. Run `hookspool list --source plaid`.
   **Check:** a queued event whose type is `<webhook_type>.<webhook_code>`. **If it fails:** read the intake log
   for `plaid <env> key fetch failed`: `error_code=INVALID_API_KEYS` means the saved credentials do not belong to
   `env`; ask the person to correct the files or the setting.
8. **Outward, rollback:** remove `[bobbins.plaid]`, run `hookspool check` and `hookspool reload`. Then ask the
   person to point the items back at their old webhook URL at Plaid. The agent does not do this.
