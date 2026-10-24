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
| `skip` | optional list of words or phrases. If one of them appears in the subject or the body, the rule does not match (the message goes on to the next rule) |
| `skip_subject` | like `skip`, but looks at the subject only. Use it for words that are fine in a footer but not in a subject (`privacy`, `survey`, `rate your`) |

Both veto lists match whole words, without regard to case: `sale` vetoes "Big sale today" but not "Wholesale Tacos" or
"Casale", and `deal` leaves "Ideal Bistro" alone. A phrase that starts or ends with a symbol, such as `% off`, still
matches "20% off". Text inside double quotes in the subject never vetoes, because that is where senders put an item name
or a saved search (`Shipped: "Sale Rack Organizer"`, `New hits for your search alert: "sale of goods"`). A quote the
sender cut off runs to the end of the subject.

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
usual. The line names the rule and the key, never the pattern or any mail.

Mail from that window is not matched later. The batch's messages still count as handled and the history id moves past
them, so once you fix the rules, mail that arrived while they were broken stays where the normal handling put it. Send a
test message right after every rules edit and look at the log.

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

### What is still missing

No pack is `verified` yet. A pack is verified when at least 95% of the service's transactional mail from its sender
domains lands on the right kind and its marketing and newsletter mail is not claimed. That has to be measured on a real
mailbox, and the measurement has not been done for any of the seven. The report to produce for each pack is: messages
matched, the count per kind, and the most common subject shapes (digits replaced by `#`, names by a placeholder) from the
service's sender domains that no rule claimed. Only counts and shapes may leave the mailbox.

Changes made without a mailbox, from the review of the first version:

* `courtlistener-alerts` lists only the alert service's own domain. Its parent organisation's domain also sends
  newsletters and appeals.
* `amazon-orders.shipped` also matches the long classic subject ("Your ... order of ... has shipped") and a first set of
  German, French, Spanish and Italian shipping subjects. The foreign-language phrases are guesses from common wording.
  Check them on real mail before relying on them.
* Veto words are matched as whole words and never inside quoted text, so a venue called "Perks Tavern" is not hidden,
  and a quote mark right after a digit is read as an inch mark (24"), not as the start of a quoted item name.

Still open: the other storefront-language subjects (receipts, delivered, out for delivery), the sender domains of each
reservation platform's transactional mail, and the per-kind recall figures.

Reservation packs share one layout. Their rules run in this order so that the general words do not swallow the specific
ones: `cancellation`, `modification`, `waitlist`, `reminder`, `confirmation`. A message whose subject asks you to rate or
review a visit is skipped.

Every pack rule asks for order or reservation context in the subject (for example a reservation word near a status
word, or `Delivered:` at the start). With `skip_subject` it vetoes subjects about policies, offers, promotions,
surveys, sign-ups, points, perks, memberships, earnings and review requests. Marketing and account mail from the same
senders is therefore left for the normal handling. No pack rule vetoes on the body, so a real order or booking whose
mail ends with "How was your delivery?" or "Recommended for you" is still claimed.

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
* Mail that arrives while the rules are broken (see the log line above) is never matched, even after the fix.
* Rules read the owner's mailbox only. Mail that reaches only the assistant's mailbox is not matched.
* Mail from a forwarding address or a mailing-list wrapper carries the forwarder's address in `From`; name that address
  in `from`.
