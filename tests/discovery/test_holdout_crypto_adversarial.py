import base64
import hashlib
import json

import pytest

from trademind.discovery.holdout_crypto import (
    HoldoutCryptoError,
    seal_bytes,
    verify_envelope,
    verify_key,
)


KEY = bytes(range(32))
MANIFEST_HASH = hashlib.sha256(b"manifest").hexdigest()
EVALUATOR_HASH = hashlib.sha256(b"evaluator").hexdigest()


def _canonical(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def test_ciphertext_tamper_cannot_pass_keyed_preflight_even_if_unkeyed_hash_is_rewritten():
    document = seal_bytes(
        b"protected-final-holdout\n",
        key=KEY,
        key_id="k1",
        hypothesis_family_id="hf_test",
        manifest_hash=MANIFEST_HASH,
        evaluator_id="eval-v1",
        evaluator_hash=EVALUATOR_HASH,
    )

    ciphertext = bytearray(base64.b64decode(document["ciphertext_b64"]))
    ciphertext[0] ^= 0x01
    document["ciphertext_b64"] = base64.b64encode(bytes(ciphertext)).decode("ascii")

    body = {
        "header": document["header"],
        "nonce_b64": document["nonce_b64"],
        "ciphertext_b64": document["ciphertext_b64"],
        "key_check": document["key_check"],
    }
    document["envelope_hash"] = hashlib.sha256(_canonical(body)).hexdigest()

    assert verify_envelope(document) is document
    with pytest.raises(HoldoutCryptoError, match="key or encrypted artifact"):
        verify_key(document, KEY)
