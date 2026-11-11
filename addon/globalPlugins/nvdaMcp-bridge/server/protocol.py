"""Bridge protocol constants and validation helpers.

All constants, error codes, dataclasses, and validation are generated from
the single source of truth at docs/protocol/protocol-constants.json.

To update: edit protocol-constants.json, then run:
    python scripts/generate_protocol.py
"""

from .protocol_generated import (  # noqa: F401  — re-export for backward compatibility
    API_LEVEL,
    BRIDGE_VERSION,
    DEFAULT_BRIDGE_TOKEN,
    ERROR_CODES,
    MAX_REQUEST_BODY_SIZE,
    MAX_REQUEST_ID_LENGTH,
    MENU_OPEN_BLOCKED_CATEGORIES,
    MODAL_BLOCKING_BLOCKED_CATEGORIES,
    PROTOCOL,
    PROTOCOL_VERSION,
    SECURE_SCREEN_ALLOWED_CATEGORIES,
    TOKEN_HEADER,
    BridgeActionEnvelope,
    BridgeError,
    BridgeMeta,
    BridgeToolConstraints,
    BridgeToolDef,
    BridgeToolsResponse,
    NvdaStatusSnapshot,
    build_tools_payload,
    http_status_from_error_code,
    validate_tools_discovery_payload,
)
