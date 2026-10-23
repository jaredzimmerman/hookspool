# Set up a service through webhook (for an agent)

The same procedure as SETUP.md, as steps. Read **Needs from the human** first. The agent never receives the
signing secret: the person writes the secret file and registers the webhook.

## Needs from the human

- The sender's webhook documentation (a link or the relevant text): where the signature or token is, what is
  signed, which field identifies the object, whether there is a delivery id, and whether the sender retries.
- The signing secret, written by the person into `/etc/hookspool/secrets/<INSTANCE>` (step 1). The agent does
  not read it, print it or repeat it.
- A name for the instance (`<INSTANCE>`, lowercase letters, digits and dashes) and the public HTTPS host
  (`<HOOKS_HOST>`).
- Approval before the webhook is created at the sender (step 5) and before it is deleted (step 7).

## Steps

1. **Goal:** the secret file exists. The person writes the secret to `/etc/hookspool/secrets/<INSTANCE>` with
   mode 0600. The agent only checks it.
   **Check:** `test -s /etc/hookspool/secrets/<INSTANCE> && ls -l /etc/hookspool/secrets/<INSTANCE>` shows mode
   `-rw-------`. **If it fails:** ask the person to write the file again.
2. **Goal:** the sender's rules are read into settings. From the documentation, choose `scheme` (hex or base64
   HMAC of the body, HMAC over body and callback URL, secret in a header, token in the URL), then `header` or
   `param`, `prefix`, `key` (the field shared by events about one object), `dedup`, `ignore` entries,
   `event_type`, `attrs` and `sender_retries`. Write down, for each, the sentence of the documentation it comes
   from. **Check:** every required choice (`scheme`, `key`) has a source sentence. **If it fails:** ask the
   person which field identifies the object, do not guess.
3. **Goal:** the instance table is in the config. Add this table, with `acme-events` replaced by `<INSTANCE>`
   everywhere in it and the settings replaced by your values:
   ```toml
   [bobbins.acme-events]
   bobbin = "webhook"
   settings = { scheme = "hmac-sha256-hex", header = "X-Signature", prefix = "sha256=", key = ["id"], event_type = "type" }
   secrets.secret = { file = "/etc/hookspool/secrets/acme-events" }
   outputs = { "*" = "hermes:acme-events" }
   ```
   **Check:** `hookspool bobbin expand <INSTANCE>` succeeds and shows the secret as a reference only.
   **If it fails:** the message is the template's own (for example `header: required by scheme ...`); fix the
   setting it names.
4. **Goal:** the config is valid and loaded. Run `hookspool check`, then `hookspool reload`.
   **Check:** no error mentions `<INSTANCE>`. **If it fails:** fix the table, do not change the bobbin.
5. **Outward, human-performed:** the person creates the webhook at the sender with the URL
   `https://<HOOKS_HOST>/hooks/<INSTANCE>` and the secret from step 1. The agent does not run this. Ask the
   person to confirm when it is done.
6. **Goal:** a real delivery is accepted. Ask the person to trigger an event at the sender, then run
   `hookspool list --source <INSTANCE>`.
   **Check:** one queued event. **If it fails:** an `unverified` result means the scheme, header, prefix or
   secret differs from the sender's; compare each with the documentation sentence from step 2.
7. **Outward, rollback:** remove `[bobbins.<INSTANCE>]`, run `hookspool check` and `hookspool reload`. Then ask the
   person to delete the webhook at the sender. The agent does not run the delete.
