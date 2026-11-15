"""Strict JSON schemas for every model call.

Structured output removes a whole class of failure (unparseable JSON, invented
fields, a uom outside the vocabulary) before our own validation even runs.
"""

from __future__ import annotations

CLASSES = (
    "rfq",
    "rfq_revision",
    "purchase_order",
    "vendor_quote",
    "status_inquiry",
    "auto_reply",
    "other",
)

UOMS = ("EA", "FT", "M", "IN", "BX", "PK", "PR", "DZ", "RL", "LB", "SET", "GAL", "BAG", "CS")

CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "classification",
        "why",
        "carries_items",
        "is_rep_forward_of_customer_email",
        "selects_items_from_quoted",
        "selected_item_labels",
        "revises_earlier_request",
        "looks_like_resend",
        "unresolvable_reference",
    ],
    "properties": {
        "classification": {"type": "string", "enum": list(CLASSES)},
        "why": {"type": "string", "description": "one short sentence a rep could check"},
        "carries_items": {
            "type": "boolean",
            "description": "true if this message is asking us to price or buy things",
        },
        "is_rep_forward_of_customer_email": {"type": "boolean"},
        "selects_items_from_quoted": {
            "type": "boolean",
            "description": "true when the message commits to or asks about specific numbered items that appear only in the quoted history",
        },
        "selected_item_labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "the item numbers/labels the message names, e.g. ['1','2','4']",
        },
        "revises_earlier_request": {"type": "boolean"},
        "looks_like_resend": {"type": "boolean"},
        "unresolvable_reference": {
            "type": ["string", "null"],
            "description": "set when the items are identified only by something not in this email",
        },
    },
}

_LINE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["raw", "description", "qty", "uom", "context_from"],
    "properties": {
        "raw": {"type": "string", "description": "the line verbatim as it appears in THIS document"},
        "description": {"type": "string", "description": "the item, self-contained, with every context that applies"},
        "qty": {"type": ["number", "null"]},
        "uom": {"type": ["string", "null"], "enum": [*UOMS, None]},
        "context_from": {
            "type": "array",
            "items": {"type": "string"},
            "description": "where inherited context came from, e.g. 'sheet name: FITTINGS - SW 3000#'",
        },
    },
}

EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_role", "notes", "unresolvable_reference", "lines"],
    "properties": {
        "document_role": {
            "type": "string",
            "enum": ["item_list", "no_items", "decorative", "unreadable"],
        },
        "notes": {"type": "string"},
        "unresolvable_reference": {"type": ["string", "null"]},
        "lines": {"type": "array", "items": _LINE},
    },
}

_MERGED_LINE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["raw", "description", "qty", "uom", "source", "change"],
    "properties": {
        "raw": {"type": "string"},
        "description": {"type": "string"},
        "qty": {"type": ["number", "null"]},
        "uom": {"type": ["string", "null"], "enum": [*UOMS, None]},
        "source": {"type": "string"},
        "change": {"type": "string", "enum": ["unchanged", "quantity_changed", "added"]},
    },
}

REVISE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lines", "removed", "notes"],
    "properties": {
        "lines": {"type": "array", "items": _MERGED_LINE},
        "removed": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}
