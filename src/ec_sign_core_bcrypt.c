/*
 Copyright 2026 B.Kats and Tom Rothamel <pytom@bishoujo.us>

 Permission is hereby granted, free of charge, to any person
 obtaining a copy of this software and associated documentation files
 (the "Software"), to deal in the Software without restriction,
 including without limitation the rights to use, copy, modify, merge,
 publish, distribute, sublicense, and/or sell copies of the Software,
 and to permit persons to whom the Software is furnished to do so,
 subject to the following conditions:

 The above copyright notice and this permission notice shall be
 included in all copies or substantial portions of the Software.

 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
*/

/*
 * ec_sign_core.h on BCrypt for Xbox, interchangeable with ec_sign_core.c (OpenSSL): P-256 ECDSA
 * over SHA-1, SEC1 DER private keys (PKCS#8 accepted), SPKI public keys, raw r || s signatures.
 */

#ifdef _WIN32

#include "ec_sign_core.h"

#include <windows.h>
#include <bcrypt.h>

#include <string.h>

#ifndef NT_SUCCESS
#define NT_SUCCESS(status) (((NTSTATUS)(status)) >= 0)
#endif

#define COORD 32
#define SHA1_LEN 20

/* 1.2.840.10045.2.1 (id-ecPublicKey) and 1.2.840.10045.3.1.7 (prime256v1). */
static const unsigned char OID_EC_PUBLIC_KEY[] = { 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01 };
static const unsigned char OID_P256[] = { 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07 };

typedef struct {
    unsigned char x[COORD];
    unsigned char y[COORD];
    unsigned char d[COORD];
} EcKey;

/*
*******************************
          DER parsing
*******************************
*/

typedef struct {
    const unsigned char *p;
    size_t len;
} Der;

/* Reads one TLV with the given tag from `in`, putting its contents in `out`. */
static int der_read(Der *in, unsigned char tag, Der *out)
{
    size_t len, n, i;

    if (in->len < 2 || in->p[0] != tag)
        return 0;

    len = in->p[1];
    n = 2;
    if (len & 0x80)
    {
        size_t bytes = len & 0x7f;
        if (bytes == 0 || bytes > 2 || in->len < 2 + bytes)
            return 0;
        len = 0;
        for (i = 0; i < bytes; i++)
            len = (len << 8) | in->p[2 + i];
        n += bytes;
    }

    if (in->len - n < len)
        return 0;

    out->p = in->p + n;
    out->len = len;
    in->p += n + len;
    in->len -= n + len;
    return 1;
}

static int der_peek(const Der *in, unsigned char tag)
{
    return in->len > 0 && in->p[0] == tag;
}

static int der_matches(Der *in, const unsigned char *expected, size_t len)
{
    if (in->len < len || memcmp(in->p, expected, len))
        return 0;
    in->p += len;
    in->len -= len;
    return 1;
}

/* AlgorithmIdentifier { id-ecPublicKey, prime256v1 } */
static int der_read_algorithm(Der *in)
{
    Der alg;
    return der_read(in, 0x30, &alg)
        && der_matches(&alg, OID_EC_PUBLIC_KEY, sizeof(OID_EC_PUBLIC_KEY))
        && der_matches(&alg, OID_P256, sizeof(OID_P256))
        && alg.len == 0;
}

/* BIT STRING with an uncompressed P-256 point. */
static int der_read_point(Der *in, EcKey *key)
{
    Der bits;
    if (!der_read(in, 0x03, &bits) || bits.len != 2 + 2 * COORD || bits.p[0] != 0x00 || bits.p[1] != 0x04)
        return 0;
    memcpy(key->x, bits.p + 2, COORD);
    memcpy(key->y, bits.p + 2 + COORD, COORD);
    return 1;
}

/* SEC1 ECPrivateKey. The public key is required: BCrypt can't derive it. */
static int parse_sec1(Der in, EcKey *key)
{
    Der seq, version, priv, ctx;

    if (!der_read(&in, 0x30, &seq) || in.len != 0)
        return 0;
    if (!der_read(&seq, 0x02, &version) || version.len != 1 || version.p[0] != 1)
        return 0;
    if (!der_read(&seq, 0x04, &priv) || priv.len == 0 || priv.len > COORD)
        return 0;

    memset(key->d, 0, COORD);
    memcpy(key->d + COORD - priv.len, priv.p, priv.len);

    if (der_peek(&seq, 0xa0))
    {
        if (!der_read(&seq, 0xa0, &ctx) || !der_matches(&ctx, OID_P256, sizeof(OID_P256)) || ctx.len != 0)
            return 0;
    }

    if (!der_read(&seq, 0xa1, &ctx) || !der_read_point(&ctx, key) || ctx.len != 0)
        return 0;

    return 1;
}

static int parse_private(const unsigned char *der, size_t len, EcKey *key)
{
    Der in = { der, len };
    Der seq, version, priv;

    if (der == NULL)
        return 0;

    if (parse_sec1(in, key))
        return 1;

    /* PKCS#8 PrivateKeyInfo { 0, AlgorithmIdentifier, OCTET STRING { ECPrivateKey } } */
    if (!der_read(&in, 0x30, &seq) || in.len != 0)
        return 0;
    if (!der_read(&seq, 0x02, &version) || version.len != 1 || version.p[0] != 0)
        return 0;
    if (!der_read_algorithm(&seq) || !der_read(&seq, 0x04, &priv))
        return 0;

    return parse_sec1(priv, key);
}

static int parse_public(const unsigned char *der, size_t len, EcKey *key)
{
    Der in = { der, len };
    Der seq;

    if (der == NULL)
        return 0;

    return der_read(&in, 0x30, &seq) && in.len == 0
        && der_read_algorithm(&seq)
        && der_read_point(&seq, key)
        && seq.len == 0;
}

/*
*******************************
          DER encoding
*******************************
*/

static unsigned char *encode_private(const EcKey *key, size_t *len)
{
    static const unsigned char prefix[] = { 0x30, 0x77, 0x02, 0x01, 0x01, 0x04, 0x20 };
    static const unsigned char params[] = { 0xa0, 0x0a, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07,
                                            0xa1, 0x44, 0x03, 0x42, 0x00, 0x04 };
    size_t n = sizeof(prefix) + COORD + sizeof(params) + 2 * COORD;
    unsigned char *der = malloc(n), *p = der;

    if (der == NULL)
        return NULL;

    memcpy(p, prefix, sizeof(prefix)); p += sizeof(prefix);
    memcpy(p, key->d, COORD); p += COORD;
    memcpy(p, params, sizeof(params)); p += sizeof(params);
    memcpy(p, key->x, COORD); p += COORD;
    memcpy(p, key->y, COORD);

    *len = n;
    return der;
}

static unsigned char *encode_public(const EcKey *key, size_t *len)
{
    static const unsigned char prefix[] = { 0x30, 0x59, 0x30, 0x13,
                                            0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01,
                                            0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07,
                                            0x03, 0x42, 0x00, 0x04 };
    size_t n = sizeof(prefix) + 2 * COORD;
    unsigned char *der = malloc(n);

    if (der == NULL)
        return NULL;

    memcpy(der, prefix, sizeof(prefix));
    memcpy(der + sizeof(prefix), key->x, COORD);
    memcpy(der + sizeof(prefix) + COORD, key->y, COORD);

    *len = n;
    return der;
}

/*
*******************************
             CNG
*******************************
*/

static BCRYPT_ALG_HANDLE open_alg(LPCWSTR name)
{
    BCRYPT_ALG_HANDLE alg = NULL;
    if (!NT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, name, NULL, 0)))
        return NULL;
    return alg;
}

static BCRYPT_KEY_HANDLE import_key(BCRYPT_ALG_HANDLE alg, const EcKey *key, int private_key)
{
    unsigned char blob[sizeof(BCRYPT_ECCKEY_BLOB) + 3 * COORD];
    BCRYPT_ECCKEY_BLOB *header = (BCRYPT_ECCKEY_BLOB *)blob;
    BCRYPT_KEY_HANDLE handle = NULL;
    ULONG size = sizeof(BCRYPT_ECCKEY_BLOB) + (private_key ? 3 : 2) * COORD;
    NTSTATUS status;

    header->dwMagic = private_key ? BCRYPT_ECDSA_PRIVATE_P256_MAGIC : BCRYPT_ECDSA_PUBLIC_P256_MAGIC;
    header->cbKey = COORD;
    memcpy(blob + sizeof(BCRYPT_ECCKEY_BLOB), key->x, COORD);
    memcpy(blob + sizeof(BCRYPT_ECCKEY_BLOB) + COORD, key->y, COORD);
    if (private_key)
        memcpy(blob + sizeof(BCRYPT_ECCKEY_BLOB) + 2 * COORD, key->d, COORD);

    /* Without BCRYPT_NO_KEY_VALIDATION, CNG checks the point and that it matches d. */
    status = BCryptImportKeyPair(alg, NULL, private_key ? BCRYPT_ECCPRIVATE_BLOB : BCRYPT_ECCPUBLIC_BLOB,
                                 &handle, blob, size, 0);
    SecureZeroMemory(blob, sizeof(blob));
    return NT_SUCCESS(status) ? handle : NULL;
}

static int sha1(const char *data, size_t data_len, unsigned char *digest)
{
    BCRYPT_ALG_HANDLE alg = open_alg(BCRYPT_SHA1_ALGORITHM);
    BCRYPT_HASH_HANDLE hash = NULL;
    int ret = 0;

    if (alg == NULL)
        return 0;

    if (NT_SUCCESS(BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0)))
    {
        ret = 1;
        /* BCryptHashData takes a ULONG length. */
        while (ret && data_len > 0)
        {
            ULONG chunk = data_len > 0x40000000 ? 0x40000000 : (ULONG)data_len;
            ret = NT_SUCCESS(BCryptHashData(hash, (PUCHAR)data, chunk, 0));
            data += chunk;
            data_len -= chunk;
        }
        ret = ret && NT_SUCCESS(BCryptFinishHash(hash, digest, SHA1_LEN, 0));
        BCryptDestroyHash(hash);
    }

    BCryptCloseAlgorithmProvider(alg, 0);
    return ret;
}

/* ec_sign_core.c's DER re-encoding of r and s fails OpenSSL's minimal-encoding check for 0x00 then < 0x80. */
static int openssl_encodable(const unsigned char *number)
{
    return !(number[0] == 0 && number[1] < 0x80);
}

/*
*******************************
      Global functions
*******************************
*/

int ECSign(const unsigned char *priv_key_der, size_t key_len, const char *data, size_t data_len, char *signature, size_t signature_len)
{
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_KEY_HANDLE handle = NULL;
    unsigned char digest[SHA1_LEN];
    unsigned char sig[2 * COORD];
    EcKey key;
    ULONG used = 0;
    int attempt, ret = 0;

    if (signature == NULL || signature_len != 64)
        return 0;
    if (!parse_private(priv_key_der, key_len, &key) || !sha1(data, data_len, digest))
        goto cleanup;

    alg = open_alg(BCRYPT_ECDSA_P256_ALGORITHM);
    if (alg == NULL || (handle = import_key(alg, &key, 1)) == NULL)
        goto cleanup;

    /* Signing is randomized, so sign again until OpenSSL can verify the result. */
    for (attempt = 0; attempt < 64; attempt++)
    {
        if (!NT_SUCCESS(BCryptSignHash(handle, NULL, digest, SHA1_LEN, sig, sizeof(sig), &used, 0)) || used != sizeof(sig))
            break;
        if (openssl_encodable(sig) && openssl_encodable(sig + COORD))
        {
            memcpy(signature, sig, sizeof(sig));
            ret = 1;
            break;
        }
    }

cleanup:
    if (handle)
        BCryptDestroyKey(handle);
    if (alg)
        BCryptCloseAlgorithmProvider(alg, 0);
    SecureZeroMemory(&key, sizeof(key));
    return ret;
}

int ECVerify(const unsigned char *public_key_der, size_t key_len, const char *data, size_t data_len, char *signature, size_t signature_len)
{
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_KEY_HANDLE handle = NULL;
    unsigned char digest[SHA1_LEN];
    EcKey key;
    int ret = 0;

    if (signature == NULL || signature_len != 64)
        return 0;
    /* Match ec_sign_core.c, which rejects these. */
    if (!openssl_encodable((unsigned char *)signature) || !openssl_encodable((unsigned char *)signature + COORD))
        return 0;
    if (!parse_public(public_key_der, key_len, &key) || !sha1(data, data_len, digest))
        return 0;

    alg = open_alg(BCRYPT_ECDSA_P256_ALGORITHM);
    if (alg != NULL && (handle = import_key(alg, &key, 0)) != NULL)
        ret = NT_SUCCESS(BCryptVerifySignature(handle, NULL, digest, SHA1_LEN, (PUCHAR)signature, 64, 0));

    if (handle)
        BCryptDestroyKey(handle);
    if (alg)
        BCryptCloseAlgorithmProvider(alg, 0);
    return ret;
}

void ECGeneratePrivateKey(unsigned char **priv_key_der, size_t *priv_len)
{
    BCRYPT_ALG_HANDLE alg = open_alg(BCRYPT_ECDSA_P256_ALGORITHM);
    BCRYPT_KEY_HANDLE handle = NULL;
    unsigned char blob[sizeof(BCRYPT_ECCKEY_BLOB) + 3 * COORD];
    ULONG used = 0;
    EcKey key;

    *priv_key_der = NULL;
    *priv_len = 0;

    if (alg == NULL)
        return;

    if (NT_SUCCESS(BCryptGenerateKeyPair(alg, &handle, 256, 0))
        && NT_SUCCESS(BCryptFinalizeKeyPair(handle, 0))
        && NT_SUCCESS(BCryptExportKey(handle, NULL, BCRYPT_ECCPRIVATE_BLOB, blob, sizeof(blob), &used, 0))
        && used == sizeof(blob)
        && ((BCRYPT_ECCKEY_BLOB *)blob)->cbKey == COORD)
    {
        memcpy(key.x, blob + sizeof(BCRYPT_ECCKEY_BLOB), COORD);
        memcpy(key.y, blob + sizeof(BCRYPT_ECCKEY_BLOB) + COORD, COORD);
        memcpy(key.d, blob + sizeof(BCRYPT_ECCKEY_BLOB) + 2 * COORD, COORD);
        *priv_key_der = encode_private(&key, priv_len);
        SecureZeroMemory(&key, sizeof(key));
    }

    SecureZeroMemory(blob, sizeof(blob));
    if (handle)
        BCryptDestroyKey(handle);
    BCryptCloseAlgorithmProvider(alg, 0);
}

void ECGetPublicKeyFromPrivate(const unsigned char *priv_key_der, size_t priv_len, unsigned char **public_key_der, size_t *pub_len)
{
    EcKey key;

    *public_key_der = NULL;
    *pub_len = 0;

    if (ECValidateKey(0, priv_key_der, priv_len) && parse_private(priv_key_der, priv_len, &key))
        *public_key_der = encode_public(&key, pub_len);

    SecureZeroMemory(&key, sizeof(key));
}

int ECValidateKey(int public, const unsigned char *key_der, size_t key_len)
{
    BCRYPT_ALG_HANDLE alg;
    BCRYPT_KEY_HANDLE handle = NULL;
    EcKey key;
    int parsed = public ? parse_public(key_der, key_len, &key) : parse_private(key_der, key_len, &key);

    if (!parsed)
        return 0;

    alg = open_alg(BCRYPT_ECDSA_P256_ALGORITHM);
    if (alg != NULL)
    {
        handle = import_key(alg, &key, !public);
        if (handle)
            BCryptDestroyKey(handle);
        BCryptCloseAlgorithmProvider(alg, 0);
    }

    SecureZeroMemory(&key, sizeof(key));
    return handle != NULL;
}

#endif /* _WIN32 */
