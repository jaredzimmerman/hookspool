# Mail rules and rule packs

Many services have no API and no webhook. They only send email. Shops tell you an order shipped, delivery
apps tell you the courier is on the way, reservation platforms confirm a table, and a court-records service mails
you when a followed case gets a new filing. The [`google.gmail`](../../hookspool/bobbins/google/gmail/README.md)
bobbin can recognise those messages without a bobbin per sender. You describe a message with a **rule**, or switch on a
ready-made **rule pack**, and every message a rule claims becomes one `mail-matched` item that carries what the rule
pulled out of it.

Nothing here uses a model. A rule is a sender, a few words or a pattern in the subject, and some regular
expressions. The matching code is `hookspool/mailrules.py`.

## What you get

A matching message produces exactly one item with the output name `mail-matched`:

| field | meaning |
|---|---|
| `rule` | the name of the rule that claimed the message (`amazon-orders.shipped`, or the name you gave your own rule) |
| `kind` | the rule's free-text `kind`: `order`, `delivery`, `receipt`, `reservation`, `cancellation`, `alert`, anything you like |
| `account` | the mailbox it arrived in |
| `message_id`, `thread_id` | Gmail's ids |
| `from`, `from_address` | the `From` header as sent, and the bare address |
| `subject` | the subject line |
| `received` | the arrival time in the instance's time zone |
| `snippet` | the first 200 characters of Gmail's snippet, or of the body text when Gmail sent none |
| `extracted` | the values the rule's `extract` table found, as `{name: text}`; a name that did not match is left out |

The usual standard fields (`source`, `rfc_message_id`, `key`) come with it. A message matches **at most one rule**, and
a message that a rule claims is not also reported as `scheduling` or `agent-mail`. The history is read once and the
message id is remembered the way it is for every other item, so a repeated push never reports the same message twice.
A pass that read new mail still reports `mailbox-changed` as before.

Rules run on new inbox mail in the **owner's** mailbox, after a deployment's `hooks.classify` hook (a hook that claims a
message wins) and before the skip rules and the scheduling check. They are not held back by `skip_domains`,
`skip_keywords` or the no-reply rule, because you named the sender on purpose. Three kinds of mail are left alone: mail
in spam or trash, mail that is not in the inbox, and mail that also names the assistant in `To` or `Cc` (the assistant
handles it). The assistant's own mailbox is not read for rules, so a message that reaches both mailboxes (a forward, a
copy) is reported once, not twice. The rules of the built-in packs also leave alone mail that Gmail filed under
promotions, social or forums; your own rules may still claim it.

## Writing a rule

A rule is a table in the `rules` setting, which is a list of tables:

| key | meaning |
|---|---|
| `name` | required, unique: becomes the item's `rule` |
| `from` | sender domains or addresses. A domain also matches its subdomains (`shop.example.com` matches `mail.shop.example.com`); an entry with an `@` is one exact address |
| `subject_any` | list of substrings, compared without regard to case. One of them in the subject is enough |
| `subject_regex` | one regular expression, case-insensitive, searched in the subject |
| `kind` | free text handed to the consumer |
| `extract` | a table of `name = "regex"`. Each regex has **exactly one** capture group. It is tried on the subject first and then on the first 20,000 characters of the body text; the first match wins, and the captured text is whitespace-trimmed and cut at 200 characters |
| `skip` | optional list of substrings. If one of them appears in the subject or the body, the rule does not match (the message goes on to the next rule) |
| `skip_subject` | like `skip`, but looks at the subject only. Use it for words that are fine in a footer but not in a subject (`privacy`, `offer`, `survey`) |

A rule needs at least one of `from`, `subject_any` and `subject_regex`. When both `subject_any` and `subject_regex` are
given, either one is enough. When `from` is given, the sender must match as well.

```toml
[bobbins.gmail]
bobbin = "google.gmail"
secrets.push_token = { file = "/etc/hookspool/secrets/gmail_push_token" }
secrets.cred_owner = { file = "/etc/hookspool/google/owner.json" }
outputs = { mail-matched = "script:mail-router", mailbox-changed = "script:mail-router" }

[bobbins.gmail.settings]
owner = "owner@example.com"
tz = "America/New_York"
rule_packs = ["resy-reservations"]

[[bobbins.gmail.settings.rules]]
name = "parcel-locker"
from = ["lockers.example.com"]
subject_any = ["ready for pickup", "pickup code"]
kind = "delivery"
skip = ["survey"]

[bobbins.gmail.settings.rules.extract]
pickup_code = 'code[:\s]+([A-Z0-9]{6})'
location = 'at ([A-Za-z0-9 ]{3,40}) (?:locker|kiosk)'
```

Use single-quoted (literal) TOML strings for patterns so backslashes need no escaping. Add as many `[[...rules]]` tables as you
need. Mistakes are caught when the batch runs: a rule without a
name, an unknown key, a regular expression that does not compile, an `extract` pattern without exactly one capture
group, two rules with the same name or an unknown pack name. The batch does not fail: all the rules are ignored for that batch,
the log gets one line, `mail rules ignored for this batch: <what is wrong>`, and scheduling and assistant mail go on as
usual. So after you edit rules, send a test message and look at the log. The line names the rule and the key, never the pattern or any mail.

Keep a rule narrow. A word like `updated` or `booking` alone also turns up in newsletters and policy notices, so ask for
the context as well (`subject_regex = 'reservation.{0,60}updated'`) and veto marketing words with `skip_subject`.

The order matters. Your own rules are tried first, in the order you wrote them. Then each pack in `rule_packs` is tried in
the order listed, and inside a pack its rules run top to bottom. Put the specific rule before the general one, and use
`skip` to keep a broad rule away from mail you do not want.

## Routing the items

Route `mail-matched` like any other output: map it to a destination in the instance's `outputs` table. The destination
then picks what to do from the `rule` field. A script destination can do it in a few lines:

```python
import json, sys

batch = json.load(sys.stdin)
for item in batch["events"]:
    rule, got = item.get("rule", ""), item.get("extracted", {})
    if rule.startswith("amazon-orders."):
        record_parcel(got.get("order_id"), item["kind"], got.get("delivery_window"))
    elif rule.endswith(".cancellation"):
        cancel_calendar_hold(got.get("confirmation_code"))
    elif item["kind"] == "alert":
        notify(item["subject"], got)
```

Match on `rule` when you know what a message is, and on `kind` when you only care about the category. A pack's rules are
named `<pack>.<rule>`.

## Rule packs

`rule_packs` is a list of pack names. A pack is a data file, `rule-packs/<name>.toml` in the bobbin's directory, with a
`[pack]` header (`name`, `status`, `description`) and `[[rules]]` tables in the same format as above. `hookspool bobbin show
google.gmail` lists the packs that ship with the installed copy.

| pack | rules | what it extracts | status |
|---|---|---|---|
| `amazon-orders` | `refunded`, `delayed`, `out-for-delivery`, `delivered`, `shipped`, `placed` | `order_id`, `item`, `delivery_window` (or `delivered_on`, `amount`) | unverified |
| `doordash-orders` | `cancelled`, `status`, `receipt` | `store`, `total`, `order_id` | unverified |
| `resy-reservations` | `cancellation`, `modification`, `waitlist`, `reminder`, `confirmation` | `restaurant`, `date_time`, `party_size`, `confirmation_code` | unverified |
| `opentable-reservations` | the same five | the same four | unverified |
| `tock-reservations` | the same five | the same four | unverified |
| `sevenrooms-reservations` | the same five | the same four | unverified |
| `courtlistener-alerts` | `docket-alert`, `search-alert` | `case_name`, `docket_number`, `court` | unverified |

**Status.** A pack marked `unverified` was written from the services' published email conventions and has not been checked
against real mail. It is a starting point: turn it on, look at what arrives, and add your own rule in front of it (or
copy the pack's rule into `rules`) where a subject or a body differs. Sender entries cover each platform's own domains; a
restaurant that sends from its own domain through the platform needs a rule of its own with its domain in `from`. A pack
becomes `verified` only after its patterns have been compared with real message shapes.

Reservation packs share one layout. Their rules run in this order so that the general words do not swallow the specific
ones: `cancellation`, `modification`, `waitlist`, `reminder`, `confirmation`. A message asking you to review a visit is
skipped.

Every pack rule asks for order or reservation context in the subject (for example a reservation word near a status
word, or `Delivered:` at the start), and vetoes subjects about policies, offers, promotions, surveys and sign-ups with
`skip_subject`. Marketing and policy mail from the same senders is therefore left for the normal handling.

## Testing a rule

Rules are plain data, so you can try them on made-up mail in a Python shell:

```python
from hookspool import mailrules

rules = mailrules.parse_rules([{"name": "t", "from": ["shop.example.com"], "subject_any": ["shipped"],
                                "extract": {"order": r"order #(\d+)"}}])
rule, got = mailrules.first_match(rules, "no-reply@shop.example.com", "Your order shipped", "order #1234")
print(rule.name, got)        # t {'order': '1234'}
```

Do not paste real mail into a test, a bug report or a log. The bobbin's own logs carry counts only.

## Limits

* Rules look at the sender address, the subject and the text of the message. They cannot read attachments or images.
* A regular expression runs over at most 20,000 characters of body text.
* `extract` returns the first match; if a pattern is too loose it will return a wrong value, so keep the pattern tight
  and test it.
* Rules read the owner's mailbox only. Mail that reaches only the assistant's mailbox is not matched.
* Mail from a forwarding address or a mailing-list wrapper carries the forwarder's address in `From`; name that address
  in `from`.
