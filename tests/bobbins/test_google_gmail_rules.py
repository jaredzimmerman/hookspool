"""Mail rules and rule packs of the ``google.gmail`` bobbin (``hookspool.mailrules`` and the processor's
``mail-matched`` output). Every message here is synthetic, built from the patterns a pack describes; no real
subject, name, address or number appears."""
from __future__ import annotations

import base64
import os
import time

import pytest

import _bobbinrig
import _googlestub
from hookspool import mailrules as MR
from hookspool.bobbins.google.gmail import processor as gm

OWNER = "owner@example.test"
AGENT = "agent@example.test"
OUTPUTS = ("scheduling", "agent-mail", "agent-schedule", "mail-matched", "mailbox-changed")
PACKS = MR.pack_dir(os.path.dirname(gm.__file__))
ALL_PACKS = ["amazon-orders", "doordash-orders", "resy-reservations", "opentable-reservations", "tock-reservations",
             "sevenrooms-reservations", "courtlistener-alerts"]


def rule(**kw):
    kw.setdefault("name", "r")
    return MR.parse_rule(kw, "rules")


def first(rules, frm, subject, body="", bulk=False):
    return MR.first_match(rules, frm, subject, body, bulk=bulk)


# ---- the engine ------------------------------------------------------------------------------------------------
def test_a_domain_matches_its_subdomains_and_an_address_only_itself():
    r = [rule(**{"from": ["shop.example.test", "boss@corp.example.test"]})]
    assert first(r, "a@shop.example.test", "x") and first(r, "a@mail.shop.example.test", "x")
    assert not first(r, "a@notshop.example.test", "x") and not first(r, "a@example.test", "x")
    assert first(r, "boss@corp.example.test", "x") and not first(r, "other@corp.example.test", "x")


def test_subject_any_and_regex_match_when_either_does_and_case_is_ignored():
    r = [rule(**{"from": ["shop.example.test"], "subject_any": ["Shipped"], "subject_regex": r"order \d+ sent"})]
    assert first(r, "a@shop.example.test", "Your parcel SHIPPED today")
    assert first(r, "a@shop.example.test", "ORDER 42 SENT")
    assert not first(r, "a@shop.example.test", "Your parcel is late")


def test_the_first_rule_in_order_wins_and_a_message_matches_one_rule():
    r = [rule(name="a", subject_any=["order"]), rule(name="b", subject_any=["order"])]
    assert first(r, "x@example.test", "an order")[0].name == "a"


def test_skip_vetoes_in_the_subject_or_the_body_and_the_next_rule_gets_a_turn():
    r = [rule(name="a", subject_any=["order"], skip=["survey"]), rule(name="b", subject_any=["order"], kind="x")]
    assert first(r, "x@example.test", "order survey")[0].name == "b"
    assert first(r, "x@example.test", "order", "please take our SURVEY")[0].name == "b"
    assert first(r[:1], "x@example.test", "order", "please take our survey") is None


def test_skip_subject_looks_at_the_subject_only():
    r = [rule(subject_any=["order"], skip_subject=["privacy"])]
    assert first(r, "x@example.test", "order privacy notice") is None
    assert first(r, "x@example.test", "order shipped", "see our Privacy Notice below")      # a footer word is fine


def test_vetoes_match_whole_words_only():
    r = [rule(subject_any=["order", "reservation"], skip_subject=["sale", "deal"], skip=["promo"])]
    for subject in ("Your order from Wholesale Tacos", "Your reservation at Casale", "Your reservation at Ideal Bistro",
                    "Your order from Promotional Goods"):
        assert first(r, "x@example.test", subject), subject
    assert first(r, "x@example.test", "order", "Visit Promoland")                         # a body word inside a name
    assert first(r, "x@example.test", "Big sale on your order") is None
    assert first(r, "x@example.test", "Order deal-of-the-week") is None                   # a hyphen is a word edge
    assert first(r, "x@example.test", "order", "use code PROMO at checkout") is None


def test_a_veto_phrase_with_a_symbol_at_its_edge_still_matches():
    r = [rule(subject_any=["order"], skip_subject=["% off"])]
    assert first(r, "x@example.test", "Your next order: 20% off") is None
    assert first(r, "x@example.test", "Your order shipped")


def test_double_quoted_text_in_the_subject_never_vetoes():
    r = [rule(subject_any=["shipped", "alert"], skip_subject=["sale", "privacy"], skip=["deal"])]
    assert first(r, "x@example.test", 'Shipped: "Sale Rack Organizer"')
    assert first(r, "x@example.test", 'Shipped: \u201cBig Deal Board Game\u201d')
    assert first(r, "x@example.test", 'New hits for your search alert: "privacy policy"')
    assert first(r, "x@example.test", 'Shipped: "Deal Kit, 12 pieces...')               # a quote the sender cut off
    assert first(r, "x@example.test", 'Sale: everything shipped "free"') is None         # outside the quotes it still vetoes


def test_an_inch_mark_is_not_a_quote():
    r = [rule(subject_any=["shipped"], skip_subject=["sale"])]
    assert first(r, "x@example.test", 'Sale: shipped 24" monitor') is None             # the inch mark does not open a quote
    assert first(r, "x@example.test", 'Shipped: 24" monitor, "sale" edition')           # a real quote still shields its text
    assert first(r, "x@example.test", 'Shipped: 24" sale monitor') is None


@pytest.mark.parametrize("field", ["subject_regex", "extract"])
def test_an_overflowing_pattern_is_a_config_error_not_a_crash(field):
    bad = "(a){99999999999}"
    raw = {"name": "r", "subject_any": ["x"]}
    raw.update({"subject_regex": bad} if field == "subject_regex" else {"extract": {"k": bad}})
    with pytest.raises(ValueError):
        MR.parse_rule(raw, "rules")


def test_a_rule_pack_entry_that_is_not_a_name_is_a_config_error():
    for bad in ([{"name": "amazon-orders"}], [["amazon-orders"]], [3]):
        with pytest.raises(ValueError, match="pack name"):
            MR.compile_rules([], bad, PACKS)


def test_builtin_pack_rules_leave_bulk_mail_alone_but_custom_rules_do_not():
    rules = MR.compile_rules([{"name": "mine", "subject_any": ["order"]}], ["amazon-orders"], PACKS)
    assert first(rules, "a@amazon.com", 'Ordered: "Widget"', bulk=True)[0].name == "mine"  # hookspool-scan: allow: the service's own sender domain
    assert first(rules[1:], "a@amazon.com", 'Ordered: "Widget"', bulk=True) is None  # hookspool-scan: allow: the service's own sender domain
    assert first(rules[1:], "a@amazon.com", 'Ordered: "Widget"')[0].name == "amazon-orders.placed"  # hookspool-scan: allow: the service's own sender domain


def test_extract_tries_the_subject_then_the_body_and_leaves_out_what_does_not_match():
    r = [rule(subject_any=["order"], extract={"num": r"order #(\d+)", "city": r"city: ([a-z ]+)",
                                              "missing": r"zzz (\d+)"})]
    _, got = first(r, "x@example.test", "order #123", "order #999\ncity: Springfield \nmore")
    assert got == {"num": "123", "city": "Springfield"}


def test_extracted_values_are_trimmed_and_capped():
    r = [rule(subject_any=["a"], extract={"v": r"v=(.*)"})]
    _, got = first(r, "x@example.test", "a", "v=" + "  x " * 200)
    assert len(got["v"]) <= MR.VALUE_LIMIT and "  " not in got["v"]


@pytest.mark.parametrize("bad", [
    {"subject_any": ["x"]},                                                    # no name
    {"name": "r"},                                                             # nothing to match on
    {"name": "r", "subject_any": "x"},                                         # not a list
    {"name": "r", "subject_regex": "(zz"},                                     # bad regex
    {"name": "r", "subject_any": ["x"], "extract": {"a": r"\d+"}},             # no capture group
    {"name": "r", "subject_any": ["x"], "extract": {"a": r"(\d)(\d)"}},        # two groups
    {"name": "r", "subject_any": ["x"], "extract": {"a": "(zz"}},
    {"name": "r", "subject_any": ["x"], "bogus": 1},
    "not a table",
])
def test_a_bad_rule_is_refused_without_quoting_its_pattern(bad):
    with pytest.raises(ValueError) as e:
        MR.parse_rule(bad, "rules")
    assert "zz" not in str(e.value) and "\\d" not in str(e.value)


def test_duplicate_names_and_unknown_packs_are_refused():
    with pytest.raises(ValueError, match="used twice"):
        MR.compile_rules([{"name": "a", "subject_any": ["x"]}, {"name": "a", "subject_any": ["y"]}], [], PACKS)
    with pytest.raises(ValueError, match="no built-in pack"):
        MR.compile_rules([], ["nope"], PACKS)
    with pytest.raises(ValueError, match="listed twice"):
        MR.compile_rules([], ["resy-reservations", "resy-reservations"], PACKS)
    with pytest.raises(ValueError):
        MR.compile_rules([], ["../x"], PACKS)


def test_custom_rules_come_before_packs():
    rules = MR.compile_rules([{"name": "mine", "from": ["resy.com"], "subject_any": ["confirmed"]}],  # hookspool-scan: allow: the service's own sender domain
                             ["resy-reservations"], PACKS)
    assert rules[0].name == "mine" and rules[1].name.startswith("resy-reservations.")
    assert first(rules, "x@resy.com", "Your reservation at Place is confirmed")[0].name == "mine"  # hookspool-scan: allow: the service's own sender domain


# ---- the packs ----------------------------------------------------------------------------------------------------
def test_every_pack_loads_and_the_listing_names_them_all():
    got = {s["name"]: s for s in MR.summaries(PACKS)}
    assert sorted(got) == sorted(ALL_PACKS)
    for s in got.values():
        assert s["status"] in MR.PACK_STATUS and s["description"] and s["rules"]
    names = [r.name for p in ALL_PACKS for r in MR.load_pack(PACKS, p).rules]
    assert len(names) == len(set(names))


AMZ = "shipment-tracking@amazon.com"  # hookspool-scan: allow: the service's own sender domain
ORD = "123-4567890-1234567"
DD = "no-reply@doordash.com"  # hookspool-scan: allow: the service's own sender domain
CL = "no-reply@courtlistener.com"  # hookspool-scan: allow: the service's own sender domain
# (pack, sender, subject, body, expected rule, expected extracted subset, expected kind)
CASES = [
    ("amazon-orders", "auto-confirm@amazon.com", 'Ordered: "Widget, blue, 2 pack"',  # hookspool-scan: allow: the service's own sender domain
     f"Order # {ORD}\nArriving Thursday, Mar 6", "placed", {"order_id": ORD, "item": "Widget, blue, 2 pack"}, "order"),
    ("amazon-orders", AMZ, 'Shipped: "Widget"', f"Order # {ORD}\nArriving Mar 6 - Mar 8",
     "shipped", {"order_id": ORD, "item": "Widget", "delivery_window": "Mar 6 - Mar 8"}, "delivery"),
    ("amazon-orders", AMZ, 'Out for delivery: "Widget"', f"Order # {ORD}\nArriving today by 8 PM",
     "out-for-delivery", {"order_id": ORD, "item": "Widget", "delivery_window": "today by 8 PM"}, "delivery"),
    ("amazon-orders", AMZ, 'Delivered: "Widget"', f"Order # {ORD}\nDelivered Mar 6",
     "delivered", {"order_id": ORD, "item": "Widget", "delivered_on": "Mar 6"}, "delivery"),
    ("amazon-orders", "order-update@amazon.co.uk", 'Delayed: "Widget"', f"Order # {ORD}\nNew delivery date: Mar 9",  # hookspool-scan: allow: the service's own sender domain
     "delayed", {"order_id": ORD, "item": "Widget", "delivery_window": "Mar 9"}, "delivery"),
    ("amazon-orders", "returns@amazon.com", "Your refund for order", f"Order # {ORD}\nRefund total: $12.50",  # hookspool-scan: allow: the service's own sender domain
     "refunded", {"order_id": ORD, "amount": "$12.50"}, "receipt"),
    ("doordash-orders", DD, "Order Confirmation for Pat from Corner Cafe", "Order number: AB12CD\nTotal: $23.40",
     "receipt", {"store": "Corner Cafe", "order_id": "AB12CD", "total": "$23.40"}, "receipt"),
    ("doordash-orders", DD, "Your order from Corner Cafe is on its way", "Order # AB12CD",
     "status", {"store": "Corner Cafe", "order_id": "AB12CD"}, "delivery"),
    ("doordash-orders", DD, "Your order from Corner Cafe was canceled", "Order # AB12CD\nRefund amount: $23.40",
     "cancelled", {"store": "Corner Cafe", "total": "$23.40"}, "cancellation"),
    ("courtlistener-alerts", CL, "New filing for Doe v. Roe (1:23-cv-04567)", "Court: Example District Court",
     "docket-alert",
     {"case_name": "Doe v. Roe", "docket_number": "1:23-cv-04567", "court": "Example District Court"}, "alert"),
    ("courtlistener-alerts", CL, "Your saved search alert has new results", "Matter: Doe v. Roe, 23-4567",
     "search-alert", {"docket_number": "23-4567"}, "alert"),
]
RES_BODY = "Date: Fri, Mar 7 at 7:30 PM\nParty size: 4\nConfirmation code: XY12AB"
RES = {"resy-reservations": "notify@resy.com", "opentable-reservations": "no-reply@opentable.com",  # hookspool-scan: allow: the service's own sender domain
       "tock-reservations": "noreply@exploretock.com", "sevenrooms-reservations": "no-reply@sevenrooms.com"}  # hookspool-scan: allow: the service's own sender domain
RES_SUBJECTS = {"confirmation": "Your reservation at Corner Bistro is confirmed",
                "reminder": "Reminder: your reservation at Corner Bistro",
                "modification": "Your reservation at Corner Bistro has been updated",
                "cancellation": "Your reservation at Corner Bistro has been canceled",
                "waitlist": "You are on the waitlist at Corner Bistro"}
RES_WANT = {"restaurant": "Corner Bistro", "date_time": "Fri, Mar 7 at 7:30 PM", "party_size": "4",
            "confirmation_code": "XY12AB"}
for _pack, _frm in RES.items():
    for _name, _subj in RES_SUBJECTS.items():
        CASES.append((_pack, _frm, _subj, RES_BODY, _name, RES_WANT,
                      "cancellation" if _name == "cancellation" else "reservation"))


@pytest.mark.parametrize("pack,frm,subject,body,name,want,kind", CASES)
def test_each_pack_rule_recognises_its_mail_and_extracts(pack, frm, subject, body, name, want, kind):
    rules = MR.compile_rules([], [pack], PACKS)
    m = first(rules, frm, subject, body)
    assert m is not None, "no rule claimed the message"
    r, got = m
    assert r.name == f"{pack}.{name}" and r.kind == kind
    for k, v in want.items():
        assert got.get(k) == v, k


def test_every_rule_of_every_pack_is_covered_by_a_case():
    covered = {f"{c[0]}.{c[4]}" for c in CASES}
    every = {r.name for p in ALL_PACKS for r in MR.load_pack(PACKS, p).rules}
    assert every <= covered, sorted(every - covered)


# Marketing, policy and sign-up subjects as the services send them (synthetic): no rule of the pack may claim any.
JUNK = {
    "amazon-orders": ["Prime Day has arrived", "Prime Day deals are here: shop now", "Your refund guarantee and more",
                      "We updated our Privacy Policy", "Your order of savings: 20% off", "Delivered to your door: fast shipping",
                      "Shipped fast: new arrivals", "Items in your cart are delayed no more", "Special offer on your next order"],
    "doordash-orders": ["Become a Dasher today", "Earn more as a Dasher", "Refund guarantee with DashPass",
                        "We updated our Terms of Service", "Your weekly offers", "Your order of the week: 30% off",
                        "Order now and get a free receipt holder", "Cancel anytime with DashPass"],
    "courtlistener-alerts": ["Your alert has been created", "Your docket alert was created", "Confirm your email address",
                             "Welcome to the service", "Your alert has been disabled", "We updated our privacy policy",
                             "New features: docket tools and more"],
}
JUNK["amazon-orders"] += ["Your Prime membership receipt", "Your rewards balance is ready", "Rate your recent order",
                          "How was your delivery?", "Write a review of your order"]
JUNK["doordash-orders"] += ["Your Dasher earnings summary", "Your DashPass receipt", "Your DashPass membership has renewed",
                            "Rate your order from Corner Cafe", "How was your order from Corner Cafe?",
                            "Your Dasher rewards this week"]
for _p in ("resy-reservations", "opentable-reservations", "tock-reservations", "sevenrooms-reservations"):
    JUNK[_p] = ["We updated our Privacy Policy", "We updated our terms of service", "Email confirmation needed",
                "Confirm your email address", "Openings coming up near you", "Booking platform news",
                "Join our waitlist for new locations", "Reservations open for next month", "Book your next table: 20% off",
                "Your account has been updated", "Special offer: your next booking", "Rate your last visit",
                "Our reservation policy has changed", "Tickets and gift cards on sale", "Take our survey about your booking",
                "Reminder: your dining points and upcoming reservation perks", "Your reservation rewards are waiting",
                "How was your reservation at Corner Bistro?", "Leave a review of your reservation at Corner Bistro"]
JUNK["opentable-reservations"] = JUNK["opentable-reservations"] + [
    "Reminder: your OpenTable points and upcoming reservation perks"]
SENDER = {"amazon-orders": "store-news@amazon.com", "doordash-orders": DD, "courtlistener-alerts": CL, **RES}  # hookspool-scan: allow: the service's own sender domain
JUNK_CASES = [(p, s) for p, subs in JUNK.items() for s in subs]


@pytest.mark.parametrize("pack,subject", JUNK_CASES)
def test_marketing_and_policy_mail_of_the_service_is_not_claimed(pack, subject):
    rules = MR.compile_rules([], [pack], PACKS)
    assert first(rules, SENDER[pack], subject, "Hello. Order # 123-4567890-1234567. Privacy Notice. Reservation info.") is None


def test_every_pack_has_marketing_cases():
    assert set(JUNK) == set(ALL_PACKS) and all(len(v) >= 6 for v in JUNK.values())


@pytest.mark.parametrize("pack,frm,subject", [
    ("amazon-orders", "deals@amazon.com", "Recommended for you: widgets"),  # hookspool-scan: allow: the service's own sender domain
    ("amazon-orders", "someone@example.test", 'Ordered: "Widget"'),                 # not Amazon
    ("doordash-orders", DD, "Your weekly offers"),
    ("resy-reservations", "notify@resy.com", "Reservations open for next month"),  # hookspool-scan: allow: the service's own sender domain
    ("resy-reservations", "pat@example.test", "Your reservation at Corner Bistro is confirmed"),
    ("courtlistener-alerts", CL, "Welcome to the service"),
])
def test_other_mail_is_not_claimed(pack, frm, subject):
    assert first(MR.compile_rules([], [pack], PACKS), frm, subject, "hello") is None


# Real order and reservation mail whose names hold a veto word, or whose body ends in a feedback or recommendations
# block: every one must still be claimed.
NAMED = [
    ("amazon-orders", AMZ, 'Shipped: "Ideal Protein Bars, 12 count"', "shipped"),
    ("amazon-orders", AMZ, 'Delivered: "Salem Candle, large"', "delivered"),
    ("amazon-orders", AMZ, 'Delivered: "Promo Poster Frame"', "delivered"),
    ("doordash-orders", DD, "Your order from Wholesale Tacos has been delivered", "status"),
    ("doordash-orders", DD, "Your order from Ideal Noodles is on its way", "status"),
    ("doordash-orders", DD, "Your Dasher is on the way with your order", "status"),
    ("courtlistener-alerts", CL, 'New hits for your search alert: "sale of goods"', "search-alert"),
    ("courtlistener-alerts", CL, "New hits for your search alert: privacy policy disputes", "search-alert"),
    ("courtlistener-alerts", CL, 'New filing for Deal v. Sale (1:23-cv-04567)', "docket-alert"),
]
for _pack, _frm in RES.items():
    for _place in ("Ideal Bistro", "Casale", "Rosales", "Five Points Grill", "Offerman House"):
        NAMED.append((_pack, _frm, f"Your reservation at {_place} is confirmed", "confirmation"))


@pytest.mark.parametrize("pack,frm,subject,name", NAMED)
def test_a_veto_word_inside_a_name_does_not_hide_real_mail(pack, frm, subject, name):
    m = first(MR.compile_rules([], [pack], PACKS), frm, subject, "hello")
    assert m is not None and m[0].name == f"{pack}.{name}"


FOOTERS = ["How was your delivery? Rate your order and your Dasher.", "Recommended for you: widgets and more",
           "Leave a review. Write a review. Rate your experience."]


@pytest.mark.parametrize("footer", FOOTERS)
@pytest.mark.parametrize("pack,frm,subject,name", [
    ("amazon-orders", AMZ, 'Delivered: "USB cable"', "delivered"),
    ("amazon-orders", "auto-confirm@amazon.com", 'Ordered: "USB cable"', "placed"),  # hookspool-scan: allow: the service's own sender domain
    ("doordash-orders", DD, "Your order from Corner Cafe has been delivered", "status"),
    ("doordash-orders", DD, "Order Confirmation for Pat from Corner Cafe", "receipt"),
    ("resy-reservations", RES["resy-reservations"], "Your reservation at Corner Bistro is confirmed", "confirmation"),
])
def test_a_feedback_or_recommendations_footer_in_the_body_does_not_veto(pack, frm, subject, name, footer):
    m = first(MR.compile_rules([], [pack], PACKS), frm, subject, f"Order # {ORD}\n{footer}")
    assert m is not None and m[0].name == f"{pack}.{name}"


@pytest.mark.parametrize("pack,frm,subject", [
    ("amazon-orders", AMZ, 'How was your delivery of "USB cable"?'),
    ("amazon-orders", AMZ, 'Rate your order: "USB cable" was delivered'),
    ("doordash-orders", DD, "Rate your order from Corner Cafe: it was delivered"),
    ("resy-reservations", RES["resy-reservations"], "How was your reservation at Corner Bistro?"),
])
def test_a_review_request_subject_is_vetoed(pack, frm, subject):
    assert first(MR.compile_rules([], [pack], PACKS), frm, subject, "Delivered. Your order is complete.") is None


def test_no_pack_rule_vetoes_on_the_body():
    assert all(not r.skip for p in ALL_PACKS for r in MR.load_pack(PACKS, p).rules)


# ---- through the processor -----------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    def build(agent=False, hooks=None, **over):
        names = ("owner", "agent") if agent else ("owner",)
        secrets = {f"cred_{n}": _bobbinrig.write_cred(tmp_path / "creds" / f"{n}.json", google.port) for n in names}
        settings = {"owner": OWNER, "tz": "UTC", **google.gmail_settings(), **over}
        if agent:
            settings["agent"] = AGENT
        return _bobbinrig.Rig(tmp_path, "google.gmail", OUTPUTS, settings, secrets, hooks=hooks)
    return build


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmsg(mid, frm, subject, text, labels=("INBOX",), snippet=None):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": OWNER}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.test>"}]
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "internalDate": str(int(time.time() * 1000)),
            "snippet": snippet if snippet is not None else text[:40],
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(text)}}}


def hist(*ids):
    return {"history": [{"messagesAdded": [{"message": {"id": i}} for i in ids]}], "historyId": "120"}


def push(rig, google, *msgs):
    rig.seed_state(gm.OWNER_STATE, {"historyId": "100", "handled": []})
    google.history = [hist(*[m["id"] for m in msgs])]
    for m in msgs:
        google.messages[m["id"]] = m
    return rig.items({"emailAddress": OWNER, "historyId": "110"})


def test_a_matching_message_becomes_one_mail_matched_item(make, google):
    rig = make(rule_packs=["amazon-orders"])
    m = gmsg("m1", "Store <auto-confirm@amazon.com>", 'Shipped: "Widget"', f"Order # {ORD}\nArriving Mar 6",  # hookspool-scan: allow: the service's own sender domain
             snippet="x" * 300)
    out = push(rig, google, m)
    assert [i["event_type"] for i in out] == ["mail-matched", "mailbox-changed"]
    it = out[0]
    assert (it["rule"], it["kind"], it["account"], it["message_id"], it["thread_id"]) == \
           ("amazon-orders.shipped", "delivery", OWNER, "m1", "t-m1")
    assert it["from"] == "Store <auto-confirm@amazon.com>" and it["subject"] == 'Shipped: "Widget"'  # hookspool-scan: allow: the service's own sender domain
    assert it["extracted"]["order_id"] == ORD and it["extracted"]["item"] == "Widget"
    assert it["received"] and it["snippet"] == "x" * 200
    assert google.count("POST", "/freebusy") == 0, "no free/busy read for a rule match"
    assert rig.get_state(gm.OWNER_STATE)["handled"] == ["m1"]


def test_a_rule_match_is_not_also_a_scheduling_request(make, google):
    rig = make(rules=[{"name": "ask", "from": ["shop.example.test"], "subject_any": ["availability"], "kind": "alert"}])
    m = gmsg("m1", "a@shop.example.test", "Your availability?", "Are you available next week? What times work?")
    out = push(rig, google, m)
    assert [i["event_type"] for i in out] == ["mail-matched", "mailbox-changed"]
    assert out[0]["rule"] == "ask" and out[0]["extracted"] == {}


def test_a_vetoed_message_falls_through_to_the_scheduling_check(make, google):
    rig = make(rules=[{"name": "ask", "from": ["shop.example.test"], "subject_any": ["availability"],
                       "skip": ["call"]}])
    m = gmsg("m1", "a@shop.example.test", "Your availability?", "Are you available for a call next week?")
    out = push(rig, google, m)
    assert [i["event_type"] for i in out] == ["scheduling", "mailbox-changed"]


def test_custom_rules_win_over_packs_and_each_message_is_reported_once(make, google):
    rig = make(rules=[{"name": "mine", "from": ["amazon.com"], "subject_any": ["shipped"], "kind": "mine"}],  # hookspool-scan: allow: the service's own sender domain
               rule_packs=["amazon-orders"])
    out = push(rig, google, gmsg("m1", "a@amazon.com", 'Shipped: "Widget"', f"Order # {ORD}"),  # hookspool-scan: allow: the service's own sender domain
               gmsg("m2", "a@amazon.com", 'Delivered: "Widget"', f"Order # {ORD}"))  # hookspool-scan: allow: the service's own sender domain
    assert [(i["event_type"], i.get("rule")) for i in out[:-1]] == \
           [("mail-matched", "mine"), ("mail-matched", "amazon-orders.delivered")]


def test_a_matched_message_is_not_reported_again_on_a_repeat_push(make, google):
    rig = make(rule_packs=["amazon-orders"])
    m = gmsg("m1", "a@amazon.com", 'Ordered: "Widget"', f"Order # {ORD}")  # hookspool-scan: allow: the service's own sender domain
    assert len(push(rig, google, m)) == 2
    google.history = [hist("m1")]
    assert rig.items({"emailAddress": OWNER, "historyId": "111"}, bid="10") == []


def test_mail_outside_the_inbox_or_in_spam_is_not_matched(make, google):
    rig = make(rule_packs=["amazon-orders"])
    out = push(rig, google, gmsg("m1", "a@amazon.com", 'Ordered: "Widget"', "x", labels=("SPAM",)),  # hookspool-scan: allow: the service's own sender domain
               gmsg("m2", "a@amazon.com", 'Ordered: "Widget"', "x", labels=("SENT",)))  # hookspool-scan: allow: the service's own sender domain
    assert [i["event_type"] for i in out] == ["mailbox-changed"]


def test_a_mistyped_pack_or_rule_drops_the_rules_and_keeps_scheduling_working(make, google):
    for n, over in enumerate(({"rule_packs": ["no-such-pack"]}, {"rules": [{"name": "r"}]},
                              {"rules": [{"name": "r", "subject_regex": "(zz"}]},
                              {"rule_packs": [{"name": "amazon-orders"}]}, {"rule_packs": [["amazon-orders"]]})):
        rig = make(**over)
        m = gmsg("m1", "Pat <pat@sender.example.test>", "Your availability?", "Are you available for a call next week?")
        rig.seed_state(gm.OWNER_STATE, {"historyId": "100", "handled": []})
        google.history = [hist("m1")]
        google.messages["m1"] = m
        rc, emits, err = rig.run({"emailAddress": OWNER, "historyId": "110"}, bid=str(n + 1))
        assert rc == 0, err
        assert [e["payload"]["event_type"] for e in emits] == ["scheduling", "mailbox-changed"]
        assert err.count("mail rules ignored for this batch") == 1 and "zz" not in err


def test_a_bad_rule_does_not_dead_letter_the_batch_even_for_a_matching_sender(make, google):
    rig = make(rule_packs=["amazon-orders", "no-such-pack"])
    out = push(rig, google, gmsg("m1", "a@amazon.com", 'Ordered: "Widget"', f"Order # {ORD}"))  # hookspool-scan: allow: the service's own sender domain
    assert [i["event_type"] for i in out] == ["mailbox-changed"]


# ---- two mailboxes, hooks, bulk mail ------------------------------------------------------------------------------
def gpush(account, history="110"):
    return {"emailAddress": account, "historyId": history}


def seed_both(rig, google, *msgs):
    for name, acct in ((gm.OWNER_STATE, OWNER), ("gmail_push_state_agent", AGENT)):
        rig.seed_state(name, {"historyId": "100", "handled": []})
    google.history = [hist(*[m["id"] for m in msgs]) for _ in range(2)]     # one history read per mailbox
    for m in msgs:
        google.messages[m["id"]] = m


def test_mail_forwarded_into_the_assistant_mailbox_is_reported_once(make, google):
    rig = make(agent=True, rule_packs=["amazon-orders"])
    m = gmsg("m1", "a@amazon.com", 'Shipped: "Widget"', f"Order # {ORD}")  # hookspool-scan: allow: the service's own sender domain
    seed_both(rig, google, m)
    rc, emits, err = rig.run(gpush(OWNER), gpush(AGENT))
    assert rc == 0, err
    got = [(e["payload"]["event_type"], e["payload"]["account"]) for e in emits]
    assert [g for g in got if g[0] == "mail-matched"] == [("mail-matched", OWNER)]
    assert ("agent-mail", AGENT) in got, "the assistant's copy is ordinary assistant mail, not a second match"


def test_mail_that_also_names_the_assistant_is_left_to_the_assistant(make, google):
    rig = make(agent=True, rule_packs=["amazon-orders"])
    m = gmsg("m1", "a@amazon.com", 'Shipped: "Widget"', f"Order # {ORD}")  # hookspool-scan: allow: the service's own sender domain
    m["payload"]["headers"].append({"name": "Cc", "value": AGENT})
    seed_both(rig, google, m)
    rc, emits, err = rig.run(gpush(OWNER), gpush(AGENT))
    assert rc == 0, err
    got = [(e["payload"]["event_type"], e["payload"]["account"]) for e in emits]
    assert "mail-matched" not in [g[0] for g in got] and got.count(("agent-mail", AGENT)) == 1


def test_rules_do_not_read_the_assistant_mailbox_alone(make, google):
    rig = make(agent=True, rule_packs=["amazon-orders"])
    m = gmsg("m1", "a@amazon.com", 'Ordered: "Widget"', f"Order # {ORD}")  # hookspool-scan: allow: the service's own sender domain
    seed_both(rig, google, m)
    rc, emits, err = rig.run(gpush(AGENT))
    assert rc == 0, err
    assert [e["payload"]["event_type"] for e in emits] == ["agent-mail", "mailbox-changed"]


HOOK = """
def classify(message, account, ctx):
    if "[hook]" in message["headers"].get("subject", ""):
        return {"event_type": "agent-schedule", "kind": "reply", "new_text": "x"}
    return None
"""


def test_a_hook_that_claims_a_message_wins_over_a_rule(make, google):
    rig = make(hooks=HOOK, rules=[{"name": "any", "from": ["shop.example.test"], "kind": "order"}])
    out = push(rig, google, gmsg("m1", "a@shop.example.test", "Shipped [hook]", "x"),
               gmsg("m2", "a@shop.example.test", "Shipped", "x"))
    assert [(i["event_type"], i.get("rule")) for i in out[:-1]] == [("agent-schedule", None), ("mail-matched", "any")]


def test_pack_rules_skip_promotions_but_a_custom_rule_can_claim_them(make, google):
    rig = make(rule_packs=["amazon-orders"], rules=[{"name": "mine", "from": ["amazon.com"], "subject_any": ["shipped"]}])  # hookspool-scan: allow: the service's own sender domain
    out = push(rig, google,
               gmsg("m1", "a@amazon.com", 'Ordered: "Widget"', "x", labels=("INBOX", "CATEGORY_PROMOTIONS")),  # hookspool-scan: allow: the service's own sender domain
               gmsg("m2", "a@amazon.com", 'Shipped: "Widget"', "x", labels=("INBOX", "CATEGORY_PROMOTIONS")),  # hookspool-scan: allow: the service's own sender domain
               gmsg("m3", "a@amazon.com", 'Ordered: "Widget"', "x", labels=("INBOX", "CATEGORY_UPDATES")))  # hookspool-scan: allow: the service's own sender domain
    assert [(i["event_type"], i.get("rule"), i.get("message_id")) for i in out[:-1]] == \
           [("mail-matched", "mine", "m2"), ("mail-matched", "amazon-orders.placed", "m3")]


def test_bobbin_show_lists_the_rule_packs(capsys, tmp_path):
    from hookspool.__main__ import main
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    assert main(["--config", str(cfg), "bobbin", "show", "google.gmail"]) == 0
    out = capsys.readouterr().out
    assert "rule packs" in out
    for p in ALL_PACKS:
        assert p in out
    assert "[unverified]" in out


# ---- shapes added after the first review (synthetic) ---------------------------------------------------------------
@pytest.mark.parametrize("subject", [
    'Your Amazon.com order of "' + "Widget, blue, assorted sizes " * 4 + '" has shipped!',  # hookspool-scan: allow: the service's own brand name in a subject
    "Your Amazon.com order of Widget has shipped!",  # hookspool-scan: allow: the service's own brand name in a subject
    "Versandbestätigung für Ihre Bestellung",
    "Ihre Bestellung wurde versandt",
    "Votre commande a été expédiée",
    "Tu pedido ha sido enviado",
    "Il tuo ordine è stato spedito",
])
def test_amazon_shipped_mail_in_the_classic_and_other_storefront_shapes(subject):
    m = first(MR.compile_rules([], ["amazon-orders"], PACKS), AMZ, subject, "Order # 123-4567890-1234567")
    assert m is not None and m[0].name == "amazon-orders.shipped"


def test_courtlistener_alerts_ignore_the_parent_organisations_domain():
    rules = MR.compile_rules([], ["courtlistener-alerts"], PACKS)
    assert first(rules, CL, "New filings in a followed case")
    assert first(rules, "news@free.law", "New filings and new results this month") is None  # hookspool-scan: allow: the service's parent domain
