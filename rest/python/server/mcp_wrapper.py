# Copyright 2026 UCP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MCP JSON-RPC 2.0 wrapper for the UCP Merchant Server."""

import logging
import json
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse
import httpx

JSON_RPC_VERSION = "2.0"

# JSON-RPC 2.0 Error Codes
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32000

logger = logging.getLogger(__name__)

# Load the detailed tool definitions from the JSON file.
try:
    with (Path(__file__).parent / "mcp_tools.json").open() as f:
        SUPPORTED_TOOLS_DEFINITION = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    logger.error("Could not load mcp_tools.json: %s", e)
    SUPPORTED_TOOLS_DEFINITION = []

def create_error_response(request_id, code, message, data=None):
    """Creates a standard JSON-RPC 2.0 error response."""
    error = {"code": code, "message": message}
    if data:
        error["data"] = data
    return JSONResponse(
        status_code=400,
        content={
            "jsonrpc": JSON_RPC_VERSION,
            "error": error,
            "id": request_id,
        },
    )

async def mcp_dispatcher(request: Request):
    """
    Handles JSON-RPC 2.0 requests and dispatches them to the appropriate
    internal REST endpoint.
    """
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        logger.warning("MCP Parse error: Invalid JSON received.")
        return create_error_response(None, INVALID_REQUEST, "Parse error")

    request_id = body.get("id")
    method = body.get("method")
    
    # Log incoming request early
    logger.info("MCP Request received (id: %s, method: %s)", request_id, method)
    logger.debug("MCP Request body: %s", body)

    if body.get("jsonrpc") != JSON_RPC_VERSION or "method" not in body:
        return create_error_response(
            request_id, INVALID_REQUEST, "Invalid Request"
        )

    method = body.get("method")
    params = body.get("params", {})

    # 1. Validate required _meta parameter
    meta = params.get("_meta", {})
    if not meta.get("ucp", {}).get("profile"):
        return create_error_response(
            request_id,
            INVALID_PARAMS,
            "Invalid params: _meta.ucp.profile is required.",
        )

    # 2. Prepare headers for internal REST call ****
    # Propagate key identifiers for security testing and idempotency
    headers = {
        "UCP-Agent": f'profile="{meta["ucp"]["profile"]}"',
        "Content-Type": "application/json",
        "Accept": "application/json",
        "request-signature": "test",  # Add required header for REST endpoint
    }
    if "request_id" in meta:
        headers["request-id"] = meta["request_id"]
    idem = params.get("idempotency_key") or meta.get("idempotency_key")
    if idem:
        headers["idempotency-key"] = idem

    # 3. Map MCP method to REST endpoint and dispatch
    base_url = str(request.base_url)
    checkout_object = params.get("checkout", {})

    # Ensure 'payment' object exists for create_checkout, as it's required by the REST endpoint
    if method == "create_checkout" and "payment" not in checkout_object:
        checkout_object["payment"] = {}

    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            logger.info("Dispatching MCP method '%s' to internal REST API", method)
            if method == "tools/list":
                return JSONResponse(
                    content={
                        "jsonrpc": JSON_RPC_VERSION,
                        "result": {"tools": SUPPORTED_TOOLS_DEFINITION},
                        "id": request_id,
                    }
                )

            if method == "create_checkout":
                # The create_checkout service only needs line_items and currency.
                # The service layer is responsible for fetching product details.
                create_payload = {
                    "line_items": checkout_object.get("line_items", []),
                    "currency": checkout_object.get("currency"),
                    # The REST endpoint requires a 'payment' object, even if empty.
                    "payment": checkout_object.get("payment", {}),
                }
                rest_response = await client.post(
                    "/checkout-sessions", json=create_payload, headers=headers
                )
            elif method == "get_checkout":
                checkout_id = params.get("id")
                if not checkout_id:
                    return create_error_response(request_id, INVALID_PARAMS, "params.id is required for get_checkout")
                rest_response = await client.get(f"/checkout-sessions/{checkout_id}", headers=headers)

            elif method == "update_checkout":
                checkout_id = params.get("id")
                if not checkout_id:
                    return create_error_response(request_id, INVALID_PARAMS, "params.id is required for update_checkout")
                rest_response = await client.put(f"/checkout-sessions/{checkout_id}", json=checkout_object, headers=headers)

            elif method == "complete_checkout":
                checkout_id = params.get("id")
                if not checkout_id:
                    return create_error_response(request_id, INVALID_PARAMS, "params.id is required for complete_checkout")
                complete_body = {
                    "payment_data": params.get("payment_data"),
                    "risk_signals": params.get("risk_signals", {})
                }
                rest_response = await client.post(
                    f"/checkout-sessions/{checkout_id}/complete",
                    json=complete_body,
                    headers=headers
                ) 
                
            elif method == "cancel_checkout":
                checkout_id = params.get("id")
                if not checkout_id:
                    return create_error_response(request_id, INVALID_PARAMS, "params.id is required for cancel_checkout")
                rest_response = await client.post(f"/checkout-sessions/{checkout_id}/cancel", json=checkout_object, headers=headers)
            else:
                return create_error_response(
                    request_id, METHOD_NOT_FOUND, f"Method not found: {method}"
                )

            # 4. Process response and format as JSON-RPC
            rest_response.raise_for_status()
            result_data = rest_response.json()
            logger.info("MCP Request successful (id: %s, method: %s)", request_id, method)

            return JSONResponse(
                content={
                    "jsonrpc": JSON_RPC_VERSION,
                    "result": result_data,
                    "id": request_id,
                }
            )

    except httpx.HTTPStatusError as e:
        # ---------------------------------------------------------------------
        # Improved REST -> JSON-RPC error mapping for security testing / MVP
        # ---------------------------------------------------------------------
        status = e.response.status_code
        text = e.response.text

        # Correlation fields (useful for evidence bundles)
        req_id = headers.get("request-id")
        idem_key = headers.get("idempotency-key")

        logger.error(
            "MCP Dispatcher failed. REST status=%s method=%s jsonrpc_id=%s request_id=%s idempotency_key=%s url=%s response=%s",
            status,
            method,
            request_id,
            req_id,
            idem_key,
            str(e.request.url) if getattr(e, "request", None) else None,
            text,
        )

        # Defaults
        error_code = INTERNAL_ERROR  # -32000
        message = "Internal server error during REST call"

        error_data = {
            "http_status": status,
            "response": text,
            "request_id": req_id,
            "idempotency_key": idem_key,
            "method": method,
            "url": str(e.request.url) if getattr(e, "request", None) else None,
            # Classification fields for your MVP suite:
            "category": "UPSTREAM_HTTP_ERROR",
            "retryable": False,
        }

        # Try to parse REST error JSON (preserve original)
        rest_error = None
        try:
            rest_error = e.response.json()
            error_data["rest_error"] = rest_error
        except Exception:
            # Non-JSON or invalid JSON body
            rest_error = None

        # Extract common UCP-ish fields if present
        if isinstance(rest_error, dict):
            detail = rest_error.get("detail")
            ucp_code = rest_error.get("code")
            if ucp_code:
                error_data["ucp_error_code"] = ucp_code
            if detail:
                message = detail  # show the upstream message to caller

        # ---------------------------------------------------------------------
        # HTTP status based classification (useful for differential testing)
        # ---------------------------------------------------------------------
        if status in (401, 403):
            # AuthN/AuthZ gate failures (A1/Z1)
            error_code = INVALID_PARAMS  # -32602 (or define a custom internal code if you prefer)
            error_data["category"] = "AUTH_DENIED"
            error_data["retryable"] = False

        elif status == 404:
            error_code = INVALID_PARAMS
            error_data["category"] = "NOT_FOUND"
            error_data["retryable"] = False

        elif status == 409:
            # Idempotency conflicts / illegal transitions / state conflicts (B1/U2)
            error_code = INVALID_PARAMS
            error_data["category"] = (
                "IDEMPOTENCY_CONFLICT"
                if error_data.get("ucp_error_code") in ("IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_KEY_REUSED")
                else "STATE_CONFLICT"
            )
            error_data["retryable"] = False

        elif status == 422:
            # Schema validation (U3)
            error_code = INVALID_PARAMS
            error_data["category"] = "SCHEMA_VALIDATION_FAILED"
            error_data["retryable"] = False

        elif status == 429:
            # Rate limiting (resilience / backoff behavior)
            error_code = INTERNAL_ERROR
            error_data["category"] = "RATE_LIMITED"
            error_data["retryable"] = True

        elif 400 <= status < 500:
            # Other client-side "business rule" rejections (U2, etc.)
            error_code = INVALID_PARAMS
            # If we have a known UCP error code, reflect it
            if error_data.get("ucp_error_code") == "INVALID_REQUEST":
                error_data["category"] = "BUSINESS_RULE_REJECTED"
            else:
                error_data["category"] = "CLIENT_ERROR"
            error_data["retryable"] = False

        else:
            # 5xx from upstream: generally retryable depending on endpoint/semantics
            error_code = INTERNAL_ERROR
            error_data["category"] = "UPSTREAM_5XX"
            error_data["retryable"] = True

        return create_error_response(
            request_id,
            error_code,
            message,
            data=error_data,
        )

    '''
    except httpx.HTTPStatusError as e:
        # Map specific REST errors to more meaningful JSON-RPC errors.
        logger.error(
            "MCP Dispatcher failed. REST call returned status %s for method %s. Response: %s",
            e.response.status_code,
            method,
            e.response.text,
        )

        error_code = INTERNAL_ERROR
        message = "Internal server error during REST call"
        error_data = {"status_code": e.response.status_code, "response": e.response.text}

        try:
            # Try to parse the UCP error response from the REST API
            rest_error = e.response.json()
            if "detail" in rest_error and "code" in rest_error:
                # If it's a client-side error (4xx), map it to Invalid Params
                if 400 <= e.response.status_code < 500:
                    error_code = INVALID_PARAMS
                message = rest_error["detail"]
                error_data["ucp_error_code"] = rest_error["code"]
        except (json.JSONDecodeError, KeyError):
            # If parsing fails, stick to the generic internal error
            pass

        return create_error_response(
            request_id, error_code, message, data=error_data
        )
    except Exception as e:
        logger.exception("An unexpected error occurred in mcp_dispatcher")
        return create_error_response(
            request_id,
            INTERNAL_ERROR,
            "An unexpected internal error occurred",
            data={"error_type": type(e).__name__, "details": str(e)},
        ) 
    '''
