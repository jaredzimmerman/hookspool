"""Signature helpers (ARCHITECTURE §10, §18): known-answer vectors per scheme computed with
an independent oracle (``hmac``/``hashlib`` directly, or a published vector), stale AND
future timestamps, and the constant-time-only check over the source tree."""
from __future__ import annotations

import ast
import base64
import glob
import hashlib
import hmac
import json
import os

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from hookspool import verify as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS = 1_800_000_000
SECRET = "test-verify-secret"
BODY = b'{"zen":"keep it logically awesome","n":1}'


def hexmac(secret, msg, algo=hashlib.sha256):
    return hmac.new(secret.encode() if isinstance(secret, str) else secret, msg, algo).hexdigest()


def b64mac(secret, msg, algo=hashlib.sha256):
    return base64.b64encode(hmac.new(secret.encode(), msg, algo).digest()).decode()


# ---- primitives -------------------------------------------------------------------------------------------
def test_equal_is_constant_time_and_rejects_empty():
    assert V.equal("abc", "abc") and V.equal(b"abc", "abc")
    assert not V.equal("abc", "abd") and not V.equal("abc", "abcd")
    for a, b in (("", ""), (None, "x"), ("x", None), (b"", b""), (1, 1)):
        assert not V.equal(a, b)


@pytest.mark.parametrize("delta,ok", [(0, True), (300, True), (-300, True), (301, False), (-301, False)])
def test_within_tolerance_is_two_sided(delta, ok):
    assert V.within_tolerance(TS, now=TS + delta) is ok
    assert V.within_tolerance(str(TS), now=TS + delta) is ok


@pytest.mark.parametrize("bad", ["", "abc", "1.5", "-5", "１２３", True, None, "9" * 13, b"x"])
def test_within_tolerance_rejects_non_integers(bad):
    assert not V.within_tolerance(bad, now=TS)


def test_hmac_digest_algorithms():
    assert V.hmac_digest(SECRET, BODY, "sha1") == hmac.new(SECRET.encode(), BODY, hashlib.sha1).digest()
    with pytest.raises(ValueError):
        V.hmac_digest(SECRET, BODY, "md5")


# ---- hex / base64 / prefixed --------------------------------------------------------------------------------
def test_hmac_prefixed_github_vector():
    good = "sha256=" + hexmac(SECRET, BODY)
    assert V.hmac_prefixed(good, "sha256=", SECRET, BODY)
    assert V.hmac_prefixed("sha256=" + hexmac(SECRET, BODY).upper(), "sha256=", SECRET, BODY)
    assert not V.hmac_prefixed(good, "sha256=", SECRET, BODY + b" ")
    assert not V.hmac_prefixed(good, "sha256=", "other", BODY)
    assert not V.hmac_prefixed(good, "sha256=", "", BODY), "an unset secret never verifies"
    assert not V.hmac_prefixed(good.replace("sha256=", "sha1="), "sha256=", SECRET, BODY)
    assert not V.hmac_prefixed(hexmac(SECRET, BODY), "sha256=", SECRET, BODY)
    assert not V.hmac_prefixed("sha256=", "sha256=", SECRET, BODY)
    assert not V.hmac_prefixed(None, "sha256=", SECRET, BODY)
    assert not V.hmac_prefixed("sha256=" + "a" * 5000, "sha256=", SECRET, BODY)


def test_github_published_vector():
    # docs.github.com "Validating webhook deliveries": secret "It's a Secret to Everybody",
    # payload "Hello, World!"
    sig = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert V.hmac_prefixed(sig, "sha256=", "It's a Secret to Everybody", b"Hello, World!")


def test_hmac_hex_kaneo_shape():
    assert V.hmac_hex(hexmac(SECRET, BODY), SECRET, BODY)
    assert V.hmac_hex(hexmac(SECRET, BODY, hashlib.sha512), SECRET, BODY, algo="sha512")
    assert not V.hmac_hex(hexmac(SECRET, BODY), SECRET, BODY, algo="sha512")
    assert not V.hmac_hex("zz" * 32, SECRET, BODY)
    assert not V.hmac_hex("", SECRET, BODY)


def test_hmac_b64_trello_shape():
    url = b"https://hooks.example.test/hooks/trello"
    sig = b64mac(SECRET, BODY + url, hashlib.sha1)
    assert V.hmac_b64(sig, SECRET, BODY + url, algo="sha1")
    assert V.hmac_b64(sig.rstrip("="), SECRET, BODY + url, algo="sha1"), "padding is optional"
    assert not V.hmac_b64(sig, SECRET, BODY, algo="sha1"), "the callback URL is part of the message"
    assert not V.hmac_b64(sig, SECRET, BODY + url), "algorithm matters"
    assert not V.hmac_b64("!!notbase64!!", SECRET, BODY + url, algo="sha1")
    raw = hmac.new(SECRET.encode(), BODY, hashlib.sha256).digest()
    assert V.hmac_b64(base64.urlsafe_b64encode(raw).decode(), SECRET, BODY, urlsafe=True)


def test_sign_helpers_match_the_oracle():
    assert V.hmac_hex_sign(SECRET, BODY) == hexmac(SECRET, BODY)
    assert V.hmac_b64_sign(SECRET, BODY, "sha1") == b64mac(SECRET, BODY, hashlib.sha1)


# ---- timestamped -------------------------------------------------------------------------------------------------
def zoom_sig(ts, body=BODY, secret=SECRET):
    return "v0=" + hexmac(secret, f"v0:{ts}:".encode() + body)


def zoom(sig, ts, now, body=BODY):
    return V.timestamped(sig, SECRET, body, now=now, timestamp=str(ts), version="v0", template="{v}:{t}:")


@pytest.mark.parametrize("delta,ok", [(0, True), (300, True), (-300, True), (301, False), (-301, False),
                                      (-86400, False)])
def test_zoom_v0_window_stale_and_future(delta, ok):
    assert zoom(zoom_sig(TS), TS, TS + delta) is ok


def test_zoom_v0_rejections():
    sig = zoom_sig(TS)
    assert not zoom(sig, TS + 1, TS), "the timestamp is signed"
    assert not zoom(sig, "abc", TS)
    assert not zoom(sig, TS, TS, body=BODY + b"x")
    assert not zoom(sig.replace("v0=", "v1="), TS, TS)
    assert not zoom("v0=", TS, TS)
    assert not V.timestamped(sig, SECRET, BODY, now=TS, timestamp=None, version="v0", template="{v}:{t}:")


def stripe(ts, body=BODY, secret=SECRET):
    return hexmac(secret, f"{ts}.".encode() + body)


def test_t_v1_header_style():
    h = f"t={TS},v1={stripe(TS)}"
    assert V.timestamped(h, SECRET, BODY, now=TS)
    assert V.timestamped(f"t={TS},v1=deadbeef,v1={stripe(TS)}", SECRET, BODY, now=TS), "rotation: any v1 matches"
    assert V.timestamped(f"t={TS}, v1={stripe(TS)}", SECRET, BODY, now=TS + 300)
    assert not V.timestamped(h, SECRET, BODY, now=TS + 301), "stale"
    assert not V.timestamped(h, SECRET, BODY, now=TS - 301), "future-dated"
    assert not V.timestamped(f"t={TS + 1},v1={stripe(TS)}", SECRET, BODY, now=TS)
    assert not V.timestamped(f"v1={stripe(TS)}", SECRET, BODY, now=TS), "no timestamp"
    assert not V.timestamped(f"t={TS}", SECRET, BODY, now=TS), "no signature"
    assert not V.timestamped(f"t={TS},v0={stripe(TS)}", SECRET, BODY, now=TS), "wrong version"
    assert V.timestamped(h, SECRET, BODY, now=TS, tolerance_s=0)
    assert not V.timestamped(h, SECRET, BODY, now=TS + 1, tolerance_s=0)
    assert V.timestamped(h, SECRET, BODY, now=TS + 1000, tolerance_s=1000)


# ---- Standard Webhooks ---------------------------------------------------------------------------------------------
# The example in the Standard Webhooks spec / Svix docs.
SW_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SW_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SW_TS = 1614265330
SW_BODY = b'{"test": 2432232314}'
SW_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_standard_webhooks_published_vector():
    assert V.standard_webhooks_sign(SW_SECRET, SW_ID, SW_TS, SW_BODY) == SW_SIG
    assert V.standard_webhooks(SW_ID, str(SW_TS), SW_SIG, SW_SECRET, SW_BODY, now=SW_TS + 5)
    assert V.standard_webhooks(SW_ID, SW_TS, "v1,bogus " + SW_SIG, SW_SECRET, SW_BODY, now=SW_TS), \
        "space-separated list: any v1 matches"


@pytest.mark.parametrize("delta,ok", [(300, True), (-300, True), (301, False), (-301, False)])
def test_standard_webhooks_window(delta, ok):
    assert V.standard_webhooks(SW_ID, str(SW_TS), SW_SIG, SW_SECRET, SW_BODY, now=SW_TS + delta) is ok


def test_standard_webhooks_rejections_and_raw_key():
    assert not V.standard_webhooks("msg_other", str(SW_TS), SW_SIG, SW_SECRET, SW_BODY, now=SW_TS)
    assert not V.standard_webhooks(SW_ID, str(SW_TS), SW_SIG, SW_SECRET, SW_BODY + b" ", now=SW_TS)
    assert not V.standard_webhooks(SW_ID, str(SW_TS), SW_SIG.replace("v1,", "v2,"), SW_SECRET, SW_BODY, now=SW_TS)
    assert not V.standard_webhooks(SW_ID, str(SW_TS), SW_SIG, "whsec_!!!", SW_BODY, now=SW_TS)
    assert not V.standard_webhooks("", str(SW_TS), SW_SIG, SW_SECRET, SW_BODY, now=SW_TS)
    expected = "v1," + base64.b64encode(hmac.new(b"raw-test-key", b"id1.100." + BODY, hashlib.sha256).digest()).decode()
    assert V.standard_webhooks_sign("raw-test-key", "id1", 100, BODY) == expected
    assert V.standard_webhooks("id1", "100", expected, "raw-test-key", BODY, now=100)
    key = base64.b64decode(SW_SECRET[6:])
    assert V.standard_webhooks_key(SW_SECRET) == key and V.standard_webhooks_key("raw") == b"raw"


# ---- static token ------------------------------------------------------------------------------------------------------
def test_static_token():
    assert V.static_token("tok-123", "tok-123")
    assert V.static_token(" tok-123 ", "tok-123")
    assert not V.static_token("tok-124", "tok-123")
    assert not V.static_token("", "")
    assert not V.static_token(None, "tok-123")
    assert not V.static_token("tok", "")


# ---- ES256 JWT (Plaid shape) ----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid="kid-1", alg="ES256", use="sig")
    return priv, jwk


PB = b'{"webhook_type":"TRANSACTIONS","item_id":"item-1"}'


def token(priv, claims, kid="kid-1", alg="ES256"):
    return pyjwt.encode(claims, priv, algorithm=alg, headers={"kid": kid})


def claims(ts=TS, body=PB, **kw):
    c = {"iat": ts, "request_body_sha256": hashlib.sha256(body).hexdigest()}
    c.update(kw)
    return c


class Keys:
    def __init__(self, jwk):
        self.keys = {"kid-1": jwk}
        self.lookups = []

    def __call__(self, kid):
        self.lookups.append(kid)
        return self.keys.get(kid)


def test_jwt_es256_ok_with_jwk_dict_json_and_key_object(keypair):
    priv, jwk = keypair
    t = token(priv, claims())
    assert V.jwt_es256(t, Keys(jwk), body=PB, now=TS + 60)
    assert V.jwt_es256(t, lambda kid: json.dumps(jwk), body=PB, now=TS)
    assert V.jwt_es256(t, lambda kid: priv.public_key(), body=PB, now=TS)


@pytest.mark.parametrize("delta,ok", [(300, True), (-300, True), (301, False), (-301, False)])
def test_jwt_es256_iat_window_is_two_sided(keypair, delta, ok):
    priv, jwk = keypair
    assert V.jwt_es256(token(priv, claims()), Keys(jwk), body=PB, now=TS + delta) is ok


def test_jwt_es256_rejections(keypair):
    priv, jwk = keypair
    keys = Keys(jwk)
    assert not V.jwt_es256(token(priv, claims()), keys, body=PB + b" ", now=TS), "body hash"
    assert not V.jwt_es256(token(priv, {"request_body_sha256": hashlib.sha256(PB).hexdigest()}), keys, body=PB,
                           now=TS), "iat required"
    assert not V.jwt_es256(token(priv, claims(exp=TS + 10)), keys, body=PB, now=TS + 20), "expired"
    assert not V.jwt_es256(token(priv, claims(nbf=TS + 100)), keys, body=PB, now=TS), "not yet valid"
    assert not V.jwt_es256(token(priv, claims(), kid="unknown"), keys, body=PB, now=TS), "unknown kid"
    other = ec.generate_private_key(ec.SECP256R1())
    assert not V.jwt_es256(token(other, claims()), keys, body=PB, now=TS), "signed by another key"
    assert not V.jwt_es256(token(priv, claims()), lambda kid: {"kty": "RSA"}, body=PB, now=TS)

    def raising(kid):
        raise OSError("fetch failed")
    assert not V.jwt_es256(token(priv, claims()), raising, body=PB, now=TS)


def test_jwt_es256_checks_structure_before_any_key_lookup(keypair):
    priv, jwk = keypair
    keys = Keys(jwk)
    hs = pyjwt.encode(claims(), "hs-test-key-that-is-long-enough-32b", algorithm="HS256", headers={"kid": "kid-1"})
    none_alg = base64.urlsafe_b64encode(b'{"alg":"none","kid":"kid-1"}').rstrip(b"=").decode() + ".e30."
    nokid = pyjwt.encode(claims(), priv, algorithm="ES256")
    for bad in (hs, none_alg, nokid, "a.b", "a.b.c.d", "", None, "eyJ.x.y", "x" * 5000, "é.é.é"):
        assert not V.jwt_es256(bad, keys, body=PB, now=TS)
    assert keys.lookups == [], "a malformed or non-ES256 token must not cause a key lookup"
    assert V.jwt_header(token(priv, claims()))["kid"] == "kid-1"


# ---- constant-time only ------------------------------------------------------------------------------------------------------
# Every == / != in verify.py, by source text. These compare public values (encodings,
# versions, JOSE header fields, part counts), never a signature, digest or token. A new
# comparison must be added here deliberately, after checking it is not secret-dependent.
ALLOWED_EQ = {
    'encoding == "hex"', 'encoding == "base64url"', 'k == "t"', "k == version", 'ver == "v1"',
    "len(parts) != 3", 'h.get("alg") != alg', 'jwk.get("kty") != "EC"',
}


def _compares(path):
    src = open(path).read()
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and any(isinstance(o, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) for o in n.ops):
            out.append(ast.get_source_segment(src, n))
    return src, tree, out


def test_verify_compares_only_public_values():
    _, tree, cmps = _compares(os.path.join(ROOT, "hookspool", "verify.py"))
    eq = {c for c in cmps if "==" in c or "!=" in c}
    assert eq == ALLOWED_EQ, f"unexpected comparison in verify.py: {sorted(eq ^ ALLOWED_EQ)}"


def test_compare_digest_only_inside_equal():
    src, tree, _ = _compares(os.path.join(ROOT, "hookspool", "verify.py"))
    users = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr == "compare_digest":
                    users.append(fn.name)
    assert users == ["equal"]
    # every helper that checks a signature ends in equal() (or delegates to one that does)
    for name in ("hmac_hex", "hmac_b64", "hmac_prefixed", "timestamped", "standard_webhooks", "static_token",
                 "jwt_es256"):
        fn = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == name)
        called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert called & {"equal", "_match_encoded"}, name


def _plugin_files():
    files = glob.glob(os.path.join(ROOT, "hookspool", "sources", "*.py"))
    files += glob.glob(os.path.join(ROOT, "hookspool", "bobbins", "*", "source.py"))
    files += glob.glob(os.path.join(ROOT, "tests", "kitplugins", "*.py"))
    # hookspool/sources/<name>.py may be a re-export of the bobbin's source (ARCHITECTURE 14.3.9): that source is listed above
    return [f for f in files if not f.endswith("__init__.py") and "sys.modules[__name__]" not in open(f).read()]


def test_plugins_use_only_the_verify_api():
    """Source plugins never compare a header to a computed value themselves and never
    build their own HMAC: the only comparison API is ``hookspool.verify``."""
    files = _plugin_files()
    assert files
    for path in files:
        src = open(path).read()
        for banned in ("compare_digest", "hmac.new", "import hmac", "hashlib."):
            assert banned not in src, f"{os.path.relpath(path, ROOT)} uses {banned}"
        tree = ast.parse(src)
        fn = next((f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "verify"), None)
        assert fn is not None, path
        for n in ast.walk(fn):
            assert not (isinstance(n, ast.Compare) and any(isinstance(o, (ast.Eq, ast.NotEq)) for o in n.ops)), \
                f"{os.path.relpath(path, ROOT)}: verify() compares with == / !="


def test_no_other_module_compares_signatures():
    for path in glob.glob(os.path.join(ROOT, "hookspool", "**", "*.py"), recursive=True):
        if path.endswith(os.path.join("hookspool", "verify.py")):
            continue
        src = open(path).read()
        assert "compare_digest" not in src, f"{path}: use hookspool.verify.equal"
