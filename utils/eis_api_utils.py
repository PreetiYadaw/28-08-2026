"""
api/utils/eis_api_utils.py

Executes generated API test case payloads against the real target API using the
EIS AES-GCM + RSA encrypted flow (adapted from EIS_API.py).
call_eis_api() always returns a string (never raises) so the caller can drop the
result straight into the "Actual_Response" column.
"""
import base64
import json
import os
import requests
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256
from Crypto.Signature import pkcs1_15

_PRIVATE_KEY_PATH    = os.getenv("EIS_PRIVATE_KEY_PATH", "./files/private_key.cer")
_EIS_PUBLIC_KEY_PATH = os.getenv("EIS_PUBLIC_KEY_PATH", "./files/ENC_EIS_UAT.cer")
_EIS_SECRET_KEY      = os.getenv("EIS_SECRET_KEY", "11111111111111111111111111111111").encode("utf-8")

_private_key = None
_eis_public_key = None


def _load_keys():
    global _private_key, _eis_public_key
    if _private_key is None:
        with open(_PRIVATE_KEY_PATH, "r") as f:
            _private_key = RSA.import_key(f.read())
    if _eis_public_key is None:
        with open(_EIS_PUBLIC_KEY_PATH, "r") as f:
            _eis_public_key = RSA.import_key(f.read())
    return _private_key, _eis_public_key


def _encrypt_aes_gcm_base64(plaintext: str, key: bytes) -> str:
    nonce = key[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(ciphertext + tag).decode("utf-8")


def _decrypt_aes_gcm_base64(encrypted_data_b64: str, key: bytes) -> str:
    decoded = base64.b64decode(encrypted_data_b64)
    tag_len = 16
    nonce = key[:12]
    ciphertext, tag = decoded[:-tag_len], decoded[-tag_len:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def _get_digital_signature(payload: str, private_key) -> str:
    h = SHA256.new(payload.encode("utf-8"))
    signature = pkcs1_15.new(private_key).sign(h)
    return base64.b64encode(signature).decode("utf-8")


def _get_access_token(eis_public_key, secret_key: bytes) -> str:
    cipher_rsa = PKCS1_OAEP.new(eis_public_key)
    encrypted = cipher_rsa.encrypt(secret_key)
    return base64.b64encode(encrypted).decode("utf-8")


def call_eis_api(payload_json: str, rrn: str, url: str, timeout: int = 60) -> str:
    """AES-GCM + RSA encrypted call. Returns decrypted, pretty-printed response text,
    a plain-text gateway error (when the API short-circuits before encrypting a
    response — e.g. missing/invalid SOURCE_ID), or a readable 'ERROR: ...' string
    for genuine failures — never raises."""
    try:
        private_key, eis_public_key = _load_keys()
    except Exception as e:
        return f"ERROR: Could not load EIS keys — {e}"

    secret_key = _EIS_SECRET_KEY
    try:
        encrypted_payload = _encrypt_aes_gcm_base64(payload_json, secret_key)
        digital_signature = _get_digital_signature(payload_json, private_key)
        access_token      = _get_access_token(eis_public_key, secret_key)
    except Exception as e:
        return f"ERROR: Encryption/signing failed — {e}"

    headers = {"Content-Type": "application/json", "AccessToken": access_token}
    payload_data = {
        "DIGI_SIGN": digital_signature,
        "REQUEST": encrypted_payload,
        "REQUEST_REFERENCE_NUMBER": rrn,
    }

    try:
        response = requests.post(url, headers=headers, json=payload_data, verify=False, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return "ERROR: Request timed out"
    except requests.exceptions.RequestException as e:
        return f"ERROR: {e}"

    try:
        req_response = response.json()
    except Exception as e:
        return f"ERROR: Could not parse response as JSON — {e}"

    # ── Gateway-level rejection: SOURCE_ID missing/invalid, malformed request,
    #    etc. — the API returns a PLAIN, UNENCRYPTED error object with no
    #    "RESPONSE" / "DIGI_SIGN" keys at all. Surface it directly instead of
    #    trying (and failing) to decrypt something that isn't there.
    if "RESPONSE" not in req_response:
        error_code = req_response.get("ERROR_CODE", "")
        error_desc = req_response.get("ERROR_DESCRIPTION", "")
        status     = req_response.get("RESPONSE_STATUS", "")
        if error_desc or error_code:
            return f"GATEWAY ERROR [{error_code}]: {error_desc}"
        # Unrecognized shape — fall back to showing the raw payload rather than
        # a confusing decrypt-failure message.
        return f"GATEWAY ERROR: Unexpected response shape — {json.dumps(req_response, ensure_ascii=False)}"

    try:
        decrypted_res_data = _decrypt_aes_gcm_base64(req_response["RESPONSE"], secret_key)
    except Exception as e:
        return f"ERROR: Could not decrypt response — {e}"

    try:
        decoded_sign = base64.b64decode(req_response["DIGI_SIGN"])
        h = SHA256.new(decrypted_res_data.encode("utf-8"))
        pkcs1_15.new(eis_public_key).verify(h, decoded_sign)
        sig_status = "valid"
    except Exception:
        sig_status = "invalid"

    try:
        pretty = json.dumps(json.loads(decrypted_res_data), indent=2, ensure_ascii=False)
    except Exception:
        pretty = decrypted_res_data

    return f"[Signature: {sig_status}]\n{pretty}"
