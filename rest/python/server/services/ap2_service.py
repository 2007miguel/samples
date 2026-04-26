import json
import base64
import logging
from typing import Any, Dict

import jcs 
import time
import httpx
import os
import re
from jwt.algorithms import get_default_algorithms
from cryptography.hazmat.primitives import serialization

from exceptions import InvalidRequestError
from models import UnifiedCheckout 
from services.ap2_verification_helpers import AP2VerificationMixin

logger = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Ap2Service(AP2VerificationMixin):
    """
    Generates ap2.merchant_authorization according to UCP AP2 Mandates Extension.

    Signs a detached JWS over the complete checkout, excluding the "ap2" field.
    Output: "<base64url-header>..<base64url-signature>"
    """

    KEY_ID = "merchant-key-2026-04"
    JWT_ALG = "ES256"

    def __init__(self, private_key_path: str = "merchant-key.pem"):
        self.private_key_path = private_key_path
        self._private_key = self._load_private_key()
        self.signing_keys = self._load_merchant_keys()

    def _load_merchant_keys(self) -> Dict[str, Any]:
        try:
            profile_path = os.path.join(os.path.dirname(__file__), "..", "routes", "discovery_profile.json")
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
            return {key.get("kid"): key for key in profile.get("signing_keys", [])}
        except Exception as e:
            logger.error("Error loading merchant signing keys: %s", e)
            return {}

    def _load_private_key(self):
        try:
            with open(self.private_key_path, "rb") as f:
                return serialization.load_pem_private_key(
                    f.read(),
                    password=None,
                )
        except Exception as e:
            logger.error("Error loading private key: %s", e)
            return None

    def _checkout_to_dict(self, checkout: UnifiedCheckout) -> Dict[str, Any]:
        """
        Converts the checkout to a dict.
        """
        if hasattr(checkout, "model_dump"):
            return checkout.model_dump(mode="json", by_alias=True)
        if isinstance(checkout, dict):
            return checkout
        raise InvalidRequestError(
            "UnifiedCheckout cannot be serialized to dict to generate merchant_authorization."
        )

    def _payload_without_ap2(self, checkout_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        UCP requires signing the checkout completely excluding the 'ap2' field.
        """
        payload = dict(checkout_dict)
        payload.pop("ap2", None)
        return payload

    def create_merchant_authorization(self, checkout: UnifiedCheckout) -> str:
        """
        Returns only ap2.merchant_authorization in detached JWS format:
        <header>..<signature>
        """
        if not self._private_key:
            raise InvalidRequestError(
                "Could not load the private key. Cannot sign merchant_authorization."
            )

        checkout_dict = self._checkout_to_dict(checkout)
        payload = self._payload_without_ap2(checkout_dict)

        if not payload:
            raise InvalidRequestError(
                "The checkout without the 'ap2' field is empty; no payload to sign."
            )

        # UCP requires a header with alg and kid
        protected_header = {
            "alg": self.JWT_ALG,
            "kid": self.KEY_ID,
        }

        # JSON Header
        header_json = json.dumps(
            protected_header,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")

        # Payload canonicalized with JCS (RFC 8785)
        canonical_payload = jcs.canonicalize(payload)

        encoded_header = _b64url(header_json)
        encoded_payload = _b64url(canonical_payload)

        # JWS signing input: header.payload
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")

        alg = get_default_algorithms()[self.JWT_ALG]
        signature = alg.sign(signing_input, self._private_key)
        encoded_signature = _b64url(signature)

        # Detached JWS: header..signature
        return f"{encoded_header}..{encoded_signature}"

    async def validate_agent_artifacts(self, checkout_mandate: str, current_checkout: UnifiedCheckout, ucp_agent: str | None = None) -> Dict[str, Any]:
        """
        Merchant-side verification for UCP AP2, executed after complete_checkout.

        Responsibilities kept in 6 steps:
        1. Ensure checkout_mandate exists
        2. Verify mandate signature (SD-JWT issuer signature)
        3. Verify key binding (KB-JWT)
        4. Verify mandate expiration
        5. Extract embedded checkout and verify ap2.merchant_authorization
        6. Compare embedded checkout terms with the current checkout session
        """
        logger.info("Validating AP2 checkout_mandate for checkout: %s", current_checkout.id)

        if ucp_agent:
            match = re.search(r'profile="([^"]+)"', ucp_agent)
            if match:
                profile_uri = match.group(1)
                logger.info("[AP2-VAL] Fetching platform signing keys from: %s", profile_uri)
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(profile_uri)
                        if response.status_code == 200:
                            data = response.json()
                            keys = data.get("ucp", {}).get("signing_keys", [])
                            self.platform_signing_keys = {k.get("kid"): k for k in keys}
                            logger.info("[AP2-VAL] Platform signing keys loaded: %s", list(self.platform_signing_keys.keys()))
                        else:
                            logger.warning("[AP2-VAL] Non-200 response fetching profile: %s", response.status_code)
                except Exception as e:
                    logger.warning("Failed to fetch platform signing keys: %s", e)
            else:
                logger.warning("[AP2-VAL] ucp_agent header present but no profile= URL found: %s", ucp_agent)
        else:
            logger.warning("[AP2-VAL] No ucp_agent header provided; platform_signing_keys will not be populated.")

        # ------------------------------------------------------------------
        # 1) Enforce mandate presence
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 1: Checking mandate presence...")
        if not checkout_mandate or not isinstance(checkout_mandate, str):
            logger.warning("[AP2-VAL] Step 1 FAILED: mandate is missing or invalid type: %s", type(checkout_mandate))
            raise InvalidRequestError("Missing or invalid ap2.checkout_mandate.")
        logger.info("[AP2-VAL] Step 1 OK: mandate present, length=%d", len(checkout_mandate))

        # ------------------------------------------------------------------
        # 2) Resolve verification key and verify the mandate signature
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 2: Resolving platform verification key...")
        logger.info("[AP2-VAL] Available platform_signing_keys: %s", list(getattr(self, 'platform_signing_keys', {}).keys()))
        logger.info("[AP2-VAL] Available signing_keys (merchant): %s", list(getattr(self, 'signing_keys', {}).keys()))
        verification_key = self.resolve_mandate_verification_key(checkout_mandate)
        if verification_key is None:
            logger.warning("[AP2-VAL] Step 2 FAILED: no matching key found for mandate. Mandate prefix: %s", checkout_mandate[:60])
            raise InvalidRequestError("Unable to resolve public key for checkout_mandate verification.")
        logger.info("[AP2-VAL] Step 2 OK: key resolved kid=%s, source=%s", verification_key.get('kid'), verification_key.get('source'))

        logger.info("[AP2-VAL] Step 2b: Verifying issuer signature...")
        verified_mandate = self.verify_checkout_mandate_signature(
            checkout_mandate=checkout_mandate,
            verification_key=verification_key,
        )
        if not verified_mandate.get("valid", False):
            logger.warning("[AP2-VAL] Step 2b FAILED: %s", verified_mandate.get('reason'))
            raise InvalidRequestError("checkout_mandate signature verification failed.")
        logger.info("[AP2-VAL] Step 2b OK: issuer signature valid. Reason: %s", verified_mandate.get('reason'))

        # ------------------------------------------------------------------
        # 3) Verify Key Binding (KB-JWT)
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 3: Verifying KB-JWT key binding...")
        kb_result = self.verify_checkout_mandate_key_binding(verified_mandate)
        if not kb_result.get("valid", False):
            logger.warning("[AP2-VAL] Step 3 FAILED: %s", kb_result.get('reason'))
            raise InvalidRequestError("checkout_mandate key binding verification failed.")
        logger.info("[AP2-VAL] Step 3 OK: key binding valid. Reason: %s", kb_result.get('reason'))

        # ------------------------------------------------------------------
        # 4) Verify mandate expiration
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 4: Verifying mandate expiration...")
        mandate_exp = verified_mandate.get("claims", {}).get("exp")
        now = int(time.time())

        if not isinstance(mandate_exp, int):
            logger.warning("[AP2-VAL] Step 4 FAILED: exp claim missing or invalid. Got: %s", mandate_exp)
            raise InvalidRequestError("checkout_mandate is missing a valid exp claim.")

        if mandate_exp < now:
            logger.warning("[AP2-VAL] Step 4 FAILED: mandate expired at %d, now=%d", mandate_exp, now)
            raise InvalidRequestError("checkout_mandate has expired.")
        logger.info("[AP2-VAL] Step 4 OK: mandate not expired (exp=%d, now=%d).", mandate_exp, now)

        # ------------------------------------------------------------------
        # 5) Extract embedded checkout and verify ap2.merchant_authorization
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 5: Extracting and verifying embedded checkout...")
        embedded_checkout = verified_mandate.get("claims", {}).get("checkout")
        if not isinstance(embedded_checkout, dict):
            logger.warning("[AP2-VAL] Step 5 FAILED: no embedded checkout in claims. Claims keys: %s", list(verified_mandate.get('claims', {}).keys()))
            raise InvalidRequestError("Verified mandate does not contain an embedded checkout object.")

        embedded_ap2 = embedded_checkout.get("ap2")
        if not isinstance(embedded_ap2, dict):
            logger.warning("[AP2-VAL] Step 5 FAILED: no ap2 object in embedded checkout. Checkout keys: %s", list(embedded_checkout.keys()))
            raise InvalidRequestError("Embedded checkout does not contain ap2 data.")

        merchant_auth = embedded_ap2.get("merchant_authorization")
        if not isinstance(merchant_auth, str) or not merchant_auth:
            logger.warning("[AP2-VAL] Step 5 FAILED: ap2.merchant_authorization missing or invalid. ap2 keys: %s", list(embedded_ap2.keys()))
            raise InvalidRequestError("Embedded checkout is missing ap2.merchant_authorization.") 
        logger.info("[AP2-VAL] Step 5: merchant_authorization found: %s", merchant_auth)

        canonical_checkout = dict(embedded_checkout)
        canonical_checkout.pop("ap2", None)
        canonicalized_jcs = jcs.canonicalize(canonical_checkout)

        merchant_sig_result = self.verify_merchant_authorization_signature(
            merchant_authorization=merchant_auth,
            canonicalized_checkout=canonicalized_jcs,
        )
        if not merchant_sig_result.get("valid", False):
            logger.warning("[AP2-VAL] Step 5 FAILED: merchant_authorization signature invalid. Reason: %s", merchant_sig_result.get('reason'))
            raise InvalidRequestError("Invalid ap2.merchant_authorization signature in embedded checkout.")
        logger.info("[AP2-VAL] Step 5 OK: merchant_authorization signature valid.")

        # ------------------------------------------------------------------
        # 6) Compare embedded checkout terms with the current session
        # ------------------------------------------------------------------
        logger.info("[AP2-VAL] Step 6: Comparing embedded checkout terms with current session...")
        if embedded_checkout.get("id") != current_checkout.id:
            logger.warning("[AP2-VAL] Step 6 FAILED: ID mismatch. embedded=%s, current=%s", embedded_checkout.get('id'), current_checkout.id)
            raise InvalidRequestError("Mismatch: embedded checkout ID does not match current session.")

        if embedded_checkout.get("currency") != current_checkout.currency:
            logger.warning("[AP2-VAL] Step 6 FAILED: currency mismatch. embedded=%s, current=%s", embedded_checkout.get('currency'), current_checkout.currency)
            raise InvalidRequestError("Mismatch: embedded checkout currency does not match current session.")

        current_totals = [t.model_dump() for t in current_checkout.totals] if current_checkout.totals else []
        if embedded_checkout.get("totals") != current_totals:
            logger.warning("[AP2-VAL] Step 6 FAILED: totals mismatch. embedded=%s, current=%s", embedded_checkout.get('totals'), current_totals)
            raise InvalidRequestError("Mismatch: embedded checkout totals do not match current session.")

        current_items = [li.model_dump() if hasattr(li, "model_dump") else {
            "id": li.id,
            "quantity": li.quantity,
        } for li in current_checkout.line_items] if current_checkout.line_items else []

        if embedded_checkout.get("line_items") != current_items:
            logger.warning("[AP2-VAL] Step 6 FAILED: line_items mismatch.")
            raise InvalidRequestError("Mismatch: embedded checkout line_items do not match current session.")
        logger.info("[AP2-VAL] Step 6 OK: all terms match.")

        return {
            "valid": True,
            "reason": "checkout_mandate verified successfully against current checkout session.",
            "embedded_checkout_id": embedded_checkout.get("id"),
            "mandate_expires_at": mandate_exp,
        }
