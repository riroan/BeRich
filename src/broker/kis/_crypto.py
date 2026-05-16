"""AES-256-CBC decryption for KIS WebSocket execution notices.

Per KIS spec, execution notice payloads are AES-256-CBC + PKCS7 + base64.
Key and IV are delivered ASCII-encoded inside the subscribe-ACK
(body.output.key / body.output.iv) and remain valid for the WS session.
"""
import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def aes_cbc_decrypt(key: str, iv: str, cipher_b64: str) -> str:
    key_bytes = key.encode("utf-8")
    iv_bytes = iv.encode("utf-8")
    cipher_bytes = base64.b64decode(cipher_b64)

    decryptor = Cipher(
        algorithms.AES(key_bytes), modes.CBC(iv_bytes),
    ).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")
