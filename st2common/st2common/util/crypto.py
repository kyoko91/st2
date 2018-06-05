# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Module for handling symmetric encryption and decryption of short values (mostly used for encrypted
datastore values aka secrets).

Note: In the past this module used and relied on keyczar, but since keyczar doesn't support Python
3, we moved to cryptography.

symmetric_encrypt and symmetric_decrypt functions except values as returned by the AESKey.Encrypt
and AESKey.Decrypt methods in keyczar. Those functions follow the same approach as keyczar methods,
but they use and rely on primitives and methods from cryptography library.

This was done to make the keyczar -> cryptography migration fully backward compatible.

Eventually, we should  move to Fernet (https://cryptography.io/en/latest/fernet/) recipe for
symmetric encryption / decryption, because it offers more robustness and safer defaults (SHA256
instead of SHA1, etc.).
"""

from __future__ import absolute_import

import os
import json
import binascii
import base64

from hashlib import sha1

import six

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.ciphers import modes
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac
from cryptography.hazmat.backends import default_backend

__all__ = [
    'read_crypto_key',

    'symmetric_encrypt',
    'symmetric_decrypt',

    'cryptography_symmetric_encrypt',
    'cryptography_symmetric_decrypt',

    # NOTE: Keyczar functions are here for testing reasons - they are only used by tests
    'keyczar_symmetric_encrypt',
    'keyczar_symmetric_decrypt',

    'AESKey'
]

# Keyczar related constants
KEYCZAR_HEADER_SIZE = 5
KEYCZAR_AES_BLOCK_SIZE = 16
KEYCZAR_HLEN = sha1().digest_size


class AESKey(object):
    """
    Class representing AES key object.
    """

    aes_key_string = None
    hmac_key_string = None
    hmac_key_size = None
    mode = None
    size = None

    def __init__(self, aes_key_string, hmac_key_string, hmac_key_size, mode='CBC', size=128):
        self.aes_key_string = aes_key_string
        self.hmac_key_string = hmac_key_string
        self.hmac_key_size = hmac_key_size
        self.mode = mode.upper()
        self.size = int(size)

        self.hmac_key_bytes = Base64WSDecode(self.hmac_key_string)
        self.aes_key_bytes = Base64WSDecode(self.aes_key_string)

    @classmethod
    def generate(self, key_size=256):
        aes_key_bytes = os.urandom(key_size / 8)
        aes_key_string = Base64WSEncode(aes_key_bytes)

        hmac_key_bytes = os.urandom(key_size / 8)
        hmac_key_string = Base64WSEncode(hmac_key_bytes)

        return AESKey(aes_key_string=aes_key_string, hmac_key_string=hmac_key_string,
                      hmac_key_size=key_size, mode='CBC', size=key_size)

    def __json__(self):
        data = {
            'hmacKey': {
                'hmacKeyString': self.hmac_key_string,
                'size': self.hmac_key_size
            },
            'aesKeyString': self.aes_key_string,
            'mode': self.mode.upper(),
            'size': int(self.size)
        }
        return json.dumps(data)

    def __repr__(self):
        return ('<AESKey hmac_key_size=%s,mode=%s,size=%s>' % (self.hmac_key_size, self.mode,
                                                               self.size))


def read_crypto_key(key_path):
    """
    Return the crypto key given a path to key file.

    :param key_path: Absolute path to file containing crypto key in Keyczar JSON format.
    :type key_path: ``str``

    :rtype: ``dict``
    """
    with open(key_path, 'r') as fp:
        content = fp.read()

    content = json.loads(content)

    aes_key = AESKey(aes_key_string=content['aesKeyString'],
                     hmac_key_string=content['hmacKey']['hmacKeyString'],
                     hmac_key_size=content['hmacKey']['size'],
                     mode=content['mode'].upper(),
                     size=content['size'])

    return aes_key


def symmetric_encrypt(encrypt_key, plaintext):
    return cryptography_symmetric_encrypt(encrypt_key=encrypt_key, plaintext=plaintext)


def symmetric_decrypt(decrypt_key, ciphertext):
    return cryptography_symmetric_decrypt(decrypt_key=decrypt_key, ciphertext=ciphertext)


def cryptography_symmetric_encrypt(encrypt_key, plaintext):
    """
    Encrypt the provided plaintext using AES encryption.

    Note 1: This function returns format fully compatible with Keyczar.Encrypt method.

    Note 2: This function is loosely based on keyczar AESKey.Encrypt (Apache 2.0 license)

    """
    assert isinstance(encrypt_key, AESKey), 'encrypt_key needs to be AESKey class instance'

    aes_key_bytes = encrypt_key.aes_key_bytes
    hmac_key_bytes = encrypt_key.hmac_key_bytes

    assert isinstance(aes_key_bytes, six.binary_type)
    assert isinstance(hmac_key_bytes, six.binary_type)

    # Pad data
    data = pkcs5_pad(plaintext)

    # Generate IV
    iv_bytes = os.urandom(KEYCZAR_AES_BLOCK_SIZE)

    backend = default_backend()
    cipher = Cipher(algorithms.AES(aes_key_bytes), modes.CBC(iv_bytes), backend=backend)
    encryptor = cipher.encryptor()

    # NOTE: We don't care about actual Keyczar header value, we only care about the length (5
    # bytes) so we simply add 5 0's
    header = '00000'

    ciphertext_bytes = encryptor.update(data) + encryptor.finalize()
    msg_bytes = header + iv_bytes + ciphertext_bytes

    # Generate HMAC signature for message (header + IV + ciphertext)
    h = hmac.HMAC(hmac_key_bytes, hashes.SHA1(), backend=backend)
    h.update(msg_bytes)
    sig_bytes = h.finalize()

    result = msg_bytes + sig_bytes

    # Convert resulting byte string to hex notation
    result = binascii.hexlify(result).upper()

    return result


def cryptography_symmetric_decrypt(decrypt_key, ciphertext):
    """
    Decrypt the provided ciphertext.

    NOTE 1: This function assumes ciphertext has been encrypted using symmetric AES crypto from
    keyczar library. Underneath it uses crypto primitives from cryptography library which is Python
    3 compatible.

    Note 2: This function is loosely based on keyczar AESKey.Decrypt (Apache 2.0 license)
    """
    assert isinstance(decrypt_key, AESKey), 'decrypt_key needs to be AESKey class instance'

    aes_key_bytes = decrypt_key.aes_key_bytes
    hmac_key_bytes = decrypt_key.hmac_key_bytes

    assert isinstance(aes_key_bytes, six.binary_type)
    assert isinstance(hmac_key_bytes, six.binary_type)

    ciphertext = binascii.unhexlify(ciphertext)

    data_bytes = ciphertext[KEYCZAR_HEADER_SIZE:]  # remove header

    # Verify ciphertext contains IV + HMAC signature
    if len(data_bytes) < KEYCZAR_AES_BLOCK_SIZE + KEYCZAR_HLEN:
        raise Exception('Invalid ciphertext (too short)')

    iv_bytes = data_bytes[:KEYCZAR_AES_BLOCK_SIZE]  # first block is IV
    ciphertext_bytes = data_bytes[KEYCZAR_AES_BLOCK_SIZE:-KEYCZAR_HLEN]  # strip IV and signature
    signature_bytes = data_bytes[-KEYCZAR_HLEN:]  # last 20 bytes are signature

    # Verify HMAC signature
    backend = default_backend()
    h = hmac.HMAC(hmac_key_bytes, hashes.SHA1(), backend=backend)
    h.update(ciphertext[:-KEYCZAR_HLEN])
    h.verify(signature_bytes)

    # Decrypt ciphertext
    cipher = Cipher(algorithms.AES(aes_key_bytes), modes.CBC(iv_bytes), backend=backend)

    decryptor = cipher.decryptor()
    decrypted = decryptor.update(ciphertext_bytes) + decryptor.finalize()

    # Unpad
    decrypted = pkcs5_unpad(decrypted)
    return decrypted

###
# NOTE: Those methods below are deprecated and only used for testing purposes
##


def keyczar_symmetric_encrypt(encrypt_key, plaintext):
    """
    Encrypt the given message using the encrypt_key. Returns a UTF-8 str
    ready to be stored in database. Note that we convert the hex notation
    to a ASCII notation to produce a UTF-8 friendly string.

    Also, this method will not return the same output on multiple invocations
    of same method. The reason is that the Encrypt method uses a different
    'Initialization Vector' per run and the IV is part of the output.

    :param encrypt_key: Symmetric AES key to use for encryption.
    :type encrypt_key: :class:`keyczar.keys.AESKey`

    :param plaintext: Plaintext / message to be encrypted.
    :type plaintext: ``str``

    :rtype: ``str``
    """
    from keyczar.keys import AesKey as KeyczarAesKey
    from keyczar.keys import HmacKey as KeyczarHmacKey
    from keyczar.keyinfo import GetMode

    encrypt_key = KeyczarAesKey(encrypt_key.aes_key_string,
                                KeyczarHmacKey(encrypt_key.hmac_key_string,
                                               encrypt_key.hmac_key_size),
                                encrypt_key.size,
                                GetMode(encrypt_key.mode))

    return binascii.hexlify(encrypt_key.Encrypt(plaintext)).upper()


def keyczar_symmetric_decrypt(decrypt_key, ciphertext):
    """
    Decrypt the given crypto text into plain text. Returns the original
    string input. Note that we first convert the string to hex notation
    and then decrypt. This is reverse of the encrypt operation.

    :param decrypt_key: Symmetric AES key to use for decryption.
    :type decrypt_key: :class:`keyczar.keys.AESKey`

    :param crypto: Crypto text to be decrypted.
    :type crypto: ``str``

    :rtype: ``str``
    """
    from keyczar.keys import AesKey as KeyczarAesKey
    from keyczar.keys import HmacKey as KeyczarHmacKey
    from keyczar.keyinfo import GetMode

    decrypt_key = KeyczarAesKey(decrypt_key.aes_key_string,
                                KeyczarHmacKey(decrypt_key.hmac_key_string,
                                               decrypt_key.hmac_key_size),
                                decrypt_key.size,
                                GetMode(decrypt_key.mode))

    return decrypt_key.Decrypt(binascii.unhexlify(ciphertext))


def pkcs5_pad(data):
    """
    Pad data using PKCS5
    """
    pad = KEYCZAR_AES_BLOCK_SIZE - len(data) % KEYCZAR_AES_BLOCK_SIZE
    data = data + pad * chr(pad)
    return data


def pkcs5_unpad(data):
    """
    Unpad data padded using PKCS5.
    """
    pad = ord(data[-1])
    data = data[:-pad]
    return data


def Base64WSEncode(s):
    """
    Return Base64 web safe encoding of s. Suppress padding characters (=).

    Uses URL-safe alphabet: - replaces +, _ replaces /. Will convert s of type
    unicode to string type first.

    @param s: string to encode as Base64
    @type s: string

    @return: Base64 representation of s.
    @rtype: string

    NOTE: Taken from keyczar (Apache 2.0 license)
    """
    return base64.urlsafe_b64encode(str(s)).replace("=", "")


def Base64WSDecode(s):
    """
    Return decoded version of given Base64 string. Ignore whitespace.

    Uses URL-safe alphabet: - replaces +, _ replaces /. Will convert s of type
    unicode to string type first.

    @param s: Base64 string to decode
    @type s: string

    @return: original string that was encoded as Base64
    @rtype: string

    @raise Base64DecodingError: If length of string (ignoring whitespace) is one
      more than a multiple of four.

    NOTE: Taken from keyczar (Apache 2.0 license)
    """
    s = ''.join(s.splitlines())
    s = str(s.replace(" ", ""))  # kill whitespace, make string (not unicode)
    d = len(s) % 4

    if d == 1:
        raise ValueError('Base64 decoding errors')
    elif d == 2:
        s += "=="
    elif d == 3:
        s += "="

    try:
        return base64.urlsafe_b64decode(s)
    except TypeError as e:
        # Decoding raises TypeError if s contains invalid characters.
        raise ValueError('Base64 decoding error: %s' % (str(e)))
