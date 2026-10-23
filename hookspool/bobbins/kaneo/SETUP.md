# Kaneo webhook setup

The Kaneo bobbin receives events from Kaneo's generic webhook integration. Kaneo signs the JSON body with
a shared secret.

## What to create

1. Choose the public callback, for example `https://hooks.example.test/hooks/kaneo`. It must be reachable
   over HTTPS and must end in `/hooks/<instance>`.
2. Generate the secret, for example `openssl rand -hex 32`. Keep it private.

## Secrets to save

- Write the secret to `/etc/hookspool/secrets/kaneo`, mode 0600, owned by the hookspool user.

## Config table

```toml
[bobbins.kaneo]
secrets.secret = { file = "/etc/hookspool/secrets/kaneo" }
outputs = { "*" = "hermes:kaneo" }
```

## Reverse proxy

Pass `/hooks/kaneo` through and do not rewrite the body. The signature covers the body byte for byte.

## Apply, then create the integration

1. `hookspool bobbin expand kaneo` prints the source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. Do this before the integration sends its first event.
3. In the Kaneo project, add a **generic webhook** integration. Set the URL to the callback from step 1, paste
   the secret, choose the events to send (task created, status changed, and so on) and save.

## Verify

1. Create or change a task in the project. `hookspool list --source kaneo` shows a queued event with the
   project id.
2. A request with a wrong signature is refused without touching the spool. Send one with the callback from
   step 1:
   `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -H 'X-Kaneo-Signature: 0000' --data '{"event":"task.created"}' https://hooks.example.test/hooks/kaneo`
   It prints `401`, and `hookspool list --source kaneo` shows no event for it.

## Retries

Whether Kaneo retries a failed delivery is not documented. Ask Kaneo (or test it with a deliberate failure) before
relying on the integration: if Kaneo does not retry, events lost while hookspool is down are lost, and a sweep
has to be designed for Kaneo's own list of events, as the github bobbin does for GitHub's delivery list. Until that
is settled, `sender_retries` stays unset.

## Roll back

Delete the `[bobbins.kaneo]` table, run `hookspool check` and `hookspool reload`, then delete the
integration in Kaneo.
