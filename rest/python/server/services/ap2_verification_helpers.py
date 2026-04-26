from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

try:
    import jcs  # type: ignore
except ImportError:  # pragma: no cover
    jcs = None

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

logger = logging.getLogger(__name__)

KeyMaterial = Union[
    Mapping[str, Any],
    str,
    bytes,
    serialization.PublicFormat,
]


class InvalidRequestError(ValueError):
    """Fallback error type for invalid AP2/UCP verification inputs."""


@dataclass(frozen=True)
class ResolvedVerificationKey:
    kid: str
    alg: str
    issuer: Optional[str]
    source: str
    public_key: Any
    raw_key_material: Any


class AP2VerificationMixin:
    """
    Drop-in helper mixin for merchant-side AP2 verification.

    Expected merchant/platform configuration patterns:

    1) Platform keys grouped by issuer
       self.platform_signing_keys_by_issuer = {
           "https://test-platform.example": {
               "platform-key-1": <PEM str|bytes or JWK dict>
           }
       }

    2) Global platform keys
       self.platform_signing_keys = {
           "platform-key-1": <PEM str|bytes or JWK dict>
       }

    3) Merchant's own published signing keys
       self.signing_keys = {
           "merchant-key-1": <PEM str|bytes or JWK dict>
       }

    Optional attributes:
    - self.expected_ap2_audience: str
    - self.allowed_jwt_algs: Iterable[str] = ("ES256",)
    - self.ap2_clock_skew_seconds: int = 60
    """

    expected_ap2_audience: Optional[str] = None
    allowed_jwt_algs: Iterable[str] = ("ES256",)
    ap2_clock_skew_seconds: int = 60

    # ------------------------------------------------------------------
    # Public methods requested by validate_agent_artifacts()
    # ------------------------------------------------------------------
    def resolve_mandate_verification_key(self, checkout_mandate: str) -> Optional[Dict[str, Any]]:
        """
        Resolve the public key that must verify the issuer-signed part of
        the AP2 SD-JWT+KB mandate.

        Strategy:
        - parse the issuer-signed JWT from the composite mandate string
        - read JOSE header to obtain kid/alg
        - read unverified claims to obtain iss
        - try issuer-scoped platform keys first
        - fall back to global platform key registries
        """
        issuer_jwt, _ = self._split_sd_jwt_kb(checkout_mandate)
        header, claims = self._parse_compact_jwt_unverified(issuer_jwt)

        kid = header.get("kid")
        alg = header.get("alg")
        iss = claims.get("iss")

        if not isinstance(kid, str) or not kid:
            return None
        if not isinstance(alg, str) or not alg:
            return None
        if alg not in set(self.allowed_jwt_algs):
            return None

        candidate_sources: list[Tuple[str, Mapping[str, Any]]] = []

        issuer_map = self._get_platform_signing_keys_for_issuer(iss)
        if issuer_map:
            candidate_sources.append((f"platform_signing_keys_by_issuer[{iss}]", issuer_map))

        global_maps = self._get_global_platform_key_maps()
        candidate_sources.extend(global_maps)

        for source_name, key_map in candidate_sources:
            raw_key = self._find_key_material_by_kid(key_map, kid)
            if raw_key is None:
                continue
            try:
                public_key = self._load_public_key(raw_key)
            except Exception:
                logger.exception("Failed loading platform verification key for kid=%s from %s", kid, source_name)
                return None

            resolved = ResolvedVerificationKey(
                kid=kid,
                alg=alg,
                issuer=iss if isinstance(iss, str) else None,
                source=source_name,
                public_key=public_key,
                raw_key_material=raw_key,
            )
            return {
                "kid": resolved.kid,
                "alg": resolved.alg,
                "issuer": resolved.issuer,
                "source": resolved.source,
                "public_key": resolved.public_key,
                "raw_key_material": resolved.raw_key_material,
            }

        return None

    def verify_checkout_mandate_signature(
        self,
        checkout_mandate: str,
        verification_key: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Verify the issuer signature of the issuer-signed JWT inside the
        checkout_mandate SD-JWT+KB string.
        """
        issuer_jwt, kb_jwt = self._split_sd_jwt_kb(checkout_mandate)
        header, claims = self._parse_compact_jwt_unverified(issuer_jwt)

        try:
            key_alg = str(verification_key["alg"])
            public_key = verification_key["public_key"]
        except Exception as exc:
            return {"valid": False, "reason": f"invalid verification_key structure: {exc}"}

        if header.get("alg") != key_alg:
            return {"valid": False, "reason": "mandate alg does not match resolved verification key alg"}

        try:
            self._verify_compact_jwt_signature(issuer_jwt, public_key, key_alg)
        except InvalidSignature:
            return {"valid": False, "reason": "issuer signature verification failed"}
        except Exception as exc:
            return {"valid": False, "reason": f"issuer signature verification error: {exc}"}

        expected_issuer = verification_key.get("issuer")
        if expected_issuer and claims.get("iss") != expected_issuer:
            return {"valid": False, "reason": "issuer claim does not match resolved key issuer"}

        expected_audience = self._get_expected_ap2_audience()
        if expected_audience is not None:
            aud_claim = claims.get("aud")
            if not self._audience_matches(aud_claim, expected_audience):
                return {
                    "valid": False,
                    "reason": f"issuer-signed mandate audience mismatch; expected {expected_audience!r}",
                }

        return {
            "valid": True,
            "reason": "issuer-signed mandate verified successfully",
            "header": header,
            "claims": claims,
            "issuer_signed_jwt": issuer_jwt,
            "kb_jwt": kb_jwt,
            "verification_key": dict(verification_key),
        }

    def verify_checkout_mandate_key_binding(self, verified_mandate: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Verify KB-JWT proof of possession using the holder public key carried
        in the already-verified issuer-signed mandate.
        """
        if not verified_mandate.get("valid", False):
            return {"valid": False, "reason": "verified_mandate must already be valid"}

        issuer_signed_jwt = verified_mandate.get("issuer_signed_jwt")
        kb_jwt = verified_mandate.get("kb_jwt")
        claims = verified_mandate.get("claims", {})

        if not isinstance(issuer_signed_jwt, str) or not issuer_signed_jwt:
            return {"valid": False, "reason": "missing issuer_signed_jwt in verified_mandate"}
        if not isinstance(kb_jwt, str) or not kb_jwt:
            return {"valid": False, "reason": "missing KB-JWT in verified_mandate"}
        if not isinstance(claims, Mapping):
            return {"valid": False, "reason": "missing verified mandate claims"}

        cnf = claims.get("cnf")
        if not isinstance(cnf, Mapping):
            return {"valid": False, "reason": "verified mandate is missing cnf claim"}

        holder_jwk = cnf.get("jwk")
        if not isinstance(holder_jwk, Mapping):
            return {"valid": False, "reason": "verified mandate cnf does not contain jwk"}

        try:
            holder_public_key = self._load_public_key(holder_jwk)
        except Exception as exc:
            return {"valid": False, "reason": f"failed loading holder public key from cnf.jwk: {exc}"}

        kb_header, kb_claims = self._parse_compact_jwt_unverified(kb_jwt)
        kb_alg = kb_header.get("alg")
        if not isinstance(kb_alg, str) or kb_alg not in set(self.allowed_jwt_algs):
            return {"valid": False, "reason": f"unsupported KB-JWT alg: {kb_alg!r}"}

        try:
            self._verify_compact_jwt_signature(kb_jwt, holder_public_key, kb_alg)
        except InvalidSignature:
            return {"valid": False, "reason": "KB-JWT signature verification failed"}
        except Exception as exc:
            return {"valid": False, "reason": f"KB-JWT verification error: {exc}"}

        sd_jwt_without_kb = issuer_signed_jwt + "~"
        expected_sd_hash = self._b64url_encode(hashlib.sha256(sd_jwt_without_kb.encode("ascii")).digest())
        actual_sd_hash = kb_claims.get("sd_hash")
        if not isinstance(actual_sd_hash, str) or actual_sd_hash != expected_sd_hash:
            return {"valid": False, "reason": "KB-JWT sd_hash mismatch"}

        expected_audience = self._get_expected_ap2_audience() or claims.get("aud")
        if expected_audience is not None and not self._audience_matches(kb_claims.get("aud"), str(expected_audience)):
            return {
                "valid": False,
                "reason": f"KB-JWT audience mismatch; expected {expected_audience!r}",
            }

        expected_nonce = claims.get("nonce")
        kb_nonce = kb_claims.get("nonce")
        if expected_nonce is not None and kb_nonce != expected_nonce:
            return {"valid": False, "reason": "KB-JWT nonce mismatch"}

        now = int(time.time())
        skew = int(getattr(self, "ap2_clock_skew_seconds", 60))

        if "exp" in kb_claims:
            if not isinstance(kb_claims["exp"], int):
                return {"valid": False, "reason": "KB-JWT exp must be an int"}
            if kb_claims["exp"] < now - skew:
                return {"valid": False, "reason": "KB-JWT has expired"}

        if "nbf" in kb_claims:
            if not isinstance(kb_claims["nbf"], int):
                return {"valid": False, "reason": "KB-JWT nbf must be an int"}
            if kb_claims["nbf"] > now + skew:
                return {"valid": False, "reason": "KB-JWT not yet valid"}

        if "iat" in kb_claims:
            if not isinstance(kb_claims["iat"], int):
                return {"valid": False, "reason": "KB-JWT iat must be an int"}
            if kb_claims["iat"] > now + skew:
                return {"valid": False, "reason": "KB-JWT iat is in the future"}

        return {
            "valid": True,
            "reason": "KB-JWT verified successfully",
            "header": kb_header,
            "claims": kb_claims,
            "holder_public_jwk": dict(holder_jwk),
        }

    def verify_merchant_authorization_signature(
        self,
        merchant_authorization: str,
        canonicalized_checkout: Union[str, bytes],
    ) -> Dict[str, Any]:
        """
        Verify the merchant's detached JWS signature over the checkout payload
        excluding the ap2 object.

        Accepts either:
        - detached compact JWS:  <protected>..<signature>
        - regular compact JWS:   <protected>.<payload>.<signature>
          (in this case the payload must match canonicalized_checkout)
        """
        if not isinstance(merchant_authorization, str) or not merchant_authorization:
            return {"valid": False, "reason": "merchant_authorization must be a non-empty string"}

        parts = merchant_authorization.split(".")
        if len(parts) != 3:
            return {"valid": False, "reason": "merchant_authorization is not a compact JWS"}

        protected_b64, payload_b64, signature_b64 = parts
        if not protected_b64 or not signature_b64:
            return {"valid": False, "reason": "merchant_authorization protected header or signature missing"}

        try:
            header = json.loads(self._b64url_decode(protected_b64))
        except Exception as exc:
            return {"valid": False, "reason": f"invalid protected header encoding: {exc}"}

        kid = header.get("kid")
        alg = header.get("alg")
        if not isinstance(kid, str) or not kid:
            return {"valid": False, "reason": "merchant_authorization header missing kid"}
        if not isinstance(alg, str) or alg not in set(self.allowed_jwt_algs):
            return {"valid": False, "reason": f"unsupported merchant_authorization alg: {alg!r}"}

        merchant_key_maps = self._get_merchant_key_maps()
        raw_key = None
        raw_key_source = None
        for source_name, key_map in merchant_key_maps:
            raw_key = self._find_key_material_by_kid(key_map, kid)
            if raw_key is not None:
                raw_key_source = source_name
                break

        if raw_key is None:
            return {"valid": False, "reason": f"unable to resolve merchant signing key for kid={kid!r}"}

        try:
            merchant_public_key = self._load_public_key(raw_key)
        except Exception as exc:
            return {"valid": False, "reason": f"failed loading merchant signing key: {exc}"}

        canonicalized_bytes = self._to_bytes(canonicalized_checkout)
        expected_payload_b64 = self._b64url_encode(canonicalized_bytes)

        if payload_b64 and payload_b64 != expected_payload_b64:
            return {"valid": False, "reason": "attached payload does not match canonicalized checkout"}

        signing_input = f"{protected_b64}.{expected_payload_b64}".encode("ascii")

        try:
            self._verify_jws_signature_bytes(signing_input, signature_b64, merchant_public_key, alg)
        except InvalidSignature:
            return {"valid": False, "reason": "merchant_authorization signature verification failed"}
        except Exception as exc:
            return {"valid": False, "reason": f"merchant_authorization verification error: {exc}"}

        return {
            "valid": True,
            "reason": "merchant_authorization verified successfully",
            "header": header,
            "kid": kid,
            "source": raw_key_source,
        }

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _split_sd_jwt_kb(self, mandate: str) -> Tuple[str, str]:
        parts = mandate.split("~")
        if len(parts) < 2:
            raise InvalidRequestError("Invalid SD-JWT+KB mandate format.")
        issuer_jwt = parts[0]
        kb_jwt = parts[-1]
        if not issuer_jwt:
            raise InvalidRequestError("checkout_mandate is missing issuer-signed JWT.")
        return issuer_jwt, kb_jwt

    def _parse_compact_jwt_unverified(self, compact_jwt: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        parts = compact_jwt.split(".")
        if len(parts) != 3:
            raise InvalidRequestError("Invalid compact JWT/JWS format.")
        header_b64, payload_b64, _ = parts
        header = json.loads(self._b64url_decode(header_b64))
        payload = json.loads(self._b64url_decode(payload_b64))
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise InvalidRequestError("JWT header and payload must be JSON objects.")
        return header, payload

    def _verify_compact_jwt_signature(self, compact_jwt: str, public_key: Any, alg: str) -> None:
        parts = compact_jwt.split(".")
        if len(parts) != 3:
            raise InvalidRequestError("Invalid compact JWT format.")
        protected_b64, payload_b64, signature_b64 = parts
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        self._verify_jws_signature_bytes(signing_input, signature_b64, public_key, alg)

    def _verify_jws_signature_bytes(self, signing_input: bytes, signature_b64: str, public_key: Any, alg: str) -> None:
        signature = self._b64url_decode_bytes(signature_b64)

        if alg == "ES256":
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                raise InvalidRequestError("ES256 requires an EC public key.")
            if len(signature) != 64:
                raise InvalidRequestError("Invalid ES256 JOSE signature length.")
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
            der_signature = encode_dss_signature(r, s)
            public_key.verify(der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
            return

        if alg == "RS256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise InvalidRequestError("RS256 requires an RSA public key.")
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return

        if alg == "PS256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise InvalidRequestError("PS256 requires an RSA public key.")
            public_key.verify(
                signature,
                signing_input,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
                hashes.SHA256(),
            )
            return

        if alg == "EdDSA":
            if isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
                public_key.verify(signature, signing_input)
                return
            raise InvalidRequestError("EdDSA requires an Ed25519 or Ed448 public key.")

        raise InvalidRequestError(f"Unsupported JWS alg: {alg}")

    def _get_platform_signing_keys_for_issuer(self, issuer: Any) -> Optional[Mapping[str, Any]]:
        if not isinstance(issuer, str) or not issuer:
            return None

        mapping = getattr(self, "platform_signing_keys_by_issuer", None)
        if isinstance(mapping, Mapping):
            keys = mapping.get(issuer)
            if isinstance(keys, Mapping):
                return keys

        getter = getattr(self, "get_platform_signing_keys_for_issuer", None)
        if callable(getter):
            keys = getter(issuer)
            if isinstance(keys, Mapping):
                return keys

        metadata_map = getattr(self, "issuer_metadata_by_issuer", None)
        if isinstance(metadata_map, Mapping):
            metadata = metadata_map.get(issuer)
            if isinstance(metadata, Mapping):
                signing_keys = metadata.get("signing_keys")
                if isinstance(signing_keys, Mapping):
                    return signing_keys
                if isinstance(signing_keys, Sequence):
                    return self._key_sequence_to_map(signing_keys)

        return None

    def _get_global_platform_key_maps(self) -> list[Tuple[str, Mapping[str, Any]]]:
        candidates: list[Tuple[str, Mapping[str, Any]]] = []
        for attr_name in ("platform_signing_keys", "trusted_platform_signing_keys"):
            value = getattr(self, attr_name, None)
            if isinstance(value, Mapping):
                candidates.append((attr_name, value))

        getter = getattr(self, "get_platform_signing_keys", None)
        if callable(getter):
            keys = getter()
            if isinstance(keys, Mapping):
                candidates.append(("get_platform_signing_keys()", keys))
        return candidates

    def _get_merchant_key_maps(self) -> list[Tuple[str, Mapping[str, Any]]]:
        candidates: list[Tuple[str, Mapping[str, Any]]] = []
        for attr_name in ("signing_keys", "merchant_signing_keys"):
            value = getattr(self, attr_name, None)
            if isinstance(value, Mapping):
                candidates.append((attr_name, value))

        getter = getattr(self, "get_merchant_signing_keys", None)
        if callable(getter):
            keys = getter()
            if isinstance(keys, Mapping):
                candidates.append(("get_merchant_signing_keys()", keys))

        return candidates

    def _find_key_material_by_kid(self, key_map: Mapping[str, Any], kid: str) -> Optional[Any]:
        if kid in key_map:
            return key_map[kid]

        keys = key_map.get("keys")
        if isinstance(keys, Sequence):
            normalized = self._key_sequence_to_map(keys)
            return normalized.get(kid)

        return None

    def _key_sequence_to_map(self, keys: Sequence[Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in keys:
            if isinstance(key, Mapping):
                maybe_kid = key.get("kid")
                if isinstance(maybe_kid, str) and maybe_kid:
                    result[maybe_kid] = key
        return result

    def _load_public_key(self, raw_key: Any) -> Any:
        if hasattr(raw_key, "verify"):
            return raw_key

        if isinstance(raw_key, Mapping):
            if "public_key_pem" in raw_key:
                return self._load_pem_public_key(self._to_bytes(raw_key["public_key_pem"]))
            if "pem" in raw_key:
                return self._load_pem_public_key(self._to_bytes(raw_key["pem"]))
            if raw_key.get("kty"):
                return self._load_public_key_from_jwk(raw_key)
            if "key" in raw_key:
                return self._load_public_key(raw_key["key"])

        if isinstance(raw_key, (str, bytes)):
            key_bytes = self._to_bytes(raw_key)
            if key_bytes.lstrip().startswith(b"-----BEGIN"):
                return self._load_pem_public_key(key_bytes)
            try:
                jwk = json.loads(key_bytes.decode("utf-8"))
            except Exception as exc:
                raise InvalidRequestError(f"Unsupported public key material encoding: {exc}") from exc
            return self._load_public_key_from_jwk(jwk)

        raise InvalidRequestError("Unsupported public key material type.")

    def _load_pem_public_key(self, key_bytes: bytes) -> Any:
        return serialization.load_pem_public_key(key_bytes)

    def _load_public_key_from_jwk(self, jwk: Mapping[str, Any]) -> Any:
        kty = jwk.get("kty")
        if kty == "EC":
            crv = jwk.get("crv")
            x = jwk.get("x")
            y = jwk.get("y")
            if crv != "P-256" or not isinstance(x, str) or not isinstance(y, str):
                raise InvalidRequestError("Only EC P-256 JWK is supported in this implementation.")
            public_numbers = ec.EllipticCurvePublicNumbers(
                int.from_bytes(self._b64url_decode_bytes(x), "big"),
                int.from_bytes(self._b64url_decode_bytes(y), "big"),
                ec.SECP256R1(),
            )
            return public_numbers.public_key()

        if kty == "RSA":
            n = jwk.get("n")
            e = jwk.get("e")
            if not isinstance(n, str) or not isinstance(e, str):
                raise InvalidRequestError("RSA JWK must contain n and e.")
            public_numbers = rsa.RSAPublicNumbers(
                int.from_bytes(self._b64url_decode_bytes(e), "big"),
                int.from_bytes(self._b64url_decode_bytes(n), "big"),
            )
            return public_numbers.public_key()

        if kty == "OKP":
            crv = jwk.get("crv")
            x = jwk.get("x")
            if crv == "Ed25519" and isinstance(x, str):
                return ed25519.Ed25519PublicKey.from_public_bytes(self._b64url_decode_bytes(x))
            raise InvalidRequestError("Only OKP Ed25519 JWK is supported in this implementation.")

        raise InvalidRequestError(f"Unsupported JWK kty: {kty!r}")

    def _get_expected_ap2_audience(self) -> Optional[str]:
        value = getattr(self, "expected_ap2_audience", None)
        if isinstance(value, str) and value:
            return value
        getter = getattr(self, "get_expected_ap2_audience", None)
        if callable(getter):
            resolved = getter()
            if isinstance(resolved, str) and resolved:
                return resolved
        return None

    def _audience_matches(self, aud_claim: Any, expected_audience: str) -> bool:
        if isinstance(aud_claim, str):
            return aud_claim == expected_audience
        if isinstance(aud_claim, Sequence) and not isinstance(aud_claim, (str, bytes, bytearray)):
            return expected_audience in aud_claim
        return False

    def _canonicalize_json(self, obj: Mapping[str, Any]) -> bytes:
        if jcs is not None:
            return jcs.canonicalize(obj)
        return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")

    def _to_bytes(self, value: Union[str, bytes, bytearray]) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        raise TypeError(f"Expected str/bytes-like value, got {type(value)!r}")

    def _b64url_decode(self, value: str) -> str:
        return self._b64url_decode_bytes(value).decode("utf-8")

    def _b64url_decode_bytes(self, value: str) -> bytes:
        padding_needed = (-len(value)) % 4
        return base64.urlsafe_b64decode(value + ("=" * padding_needed))

    def _b64url_encode(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
