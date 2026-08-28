"""
api/utils/api_testcase_utils.py

Generates positive/negative API test cases from a single-API JSON spec
(api_name, api_url, method, payload{field: {value, required, validation}}).

Design constraints:
  - Exactly ONE field is mutated per generated NEGATIVE test case (no permutations).
  - A field's "value" may be a single value OR a list of valid values. Only values
    already present in that list may ever be used in a POSITIVE test case — the LLM
    is never allowed to invent a new "correct" value.
  - 4 mandatory fields are auto-injected / validated before the LLM ever sees the
    payload: REQUEST_AUTH_ID, REQUEST_TELLER_ID, BRANCH_CODE (auto-filled with
    defaults if missing) and SOURCE_ID (hard error if missing). REQUEST_REFERENCE_NUMBER
    (RRN) is derived from SOURCE_ID unless already supplied.
"""
import json
import uuid
from typing import Dict, Any, List, Optional

from api.utils.azure_utility import client, MODEL


# ============================================================================
# Mandatory field handling
# ============================================================================
MANDATORY_DEFAULT_FIELDS: Dict[str, Dict[str, Any]] = {
    "REQUEST_AUTH_ID": {
        "value": "1036662",
        "required": True,
        "validation": "type should be string, maxlength should be 7",
    },
    "REQUEST_TELLER_ID": {
        "value": "1015421",
        "required": True,
        "validation": "type should be string, maxlength should be 7",
    },
    "BRANCH_CODE": {
        "value": "00437",
        "required": True,
        "validation": "type should be string, maxlength should be 5",
    },
}
SOURCE_ID_FIELD = "SOURCE_ID"
RRN_FIELD = "REQUEST_REFERENCE_NUMBER"


RRN_TOTAL_LENGTH = 25
RRN_PREFIX = "SBI"


def make_rrn(source_id: str) -> str:
    """
    Builds a REQUEST_REFERENCE_NUMBER that is ALWAYS exactly RRN_TOTAL_LENGTH (25)
    characters: "SBI" + SOURCE_ID + a random alphanumeric suffix that fills the
    remaining length exactly.
    """
    prefix = f"{RRN_PREFIX}{source_id}"
    remaining = RRN_TOTAL_LENGTH - len(prefix)

    if remaining <= 0:
        raise ValueError(
            f"SOURCE_ID '{source_id}' is too long — 'SBI' + SOURCE_ID must leave room "
            f"for a reference suffix within {RRN_TOTAL_LENGTH} characters "
            f"(currently {len(prefix)} chars used, 0 remaining)."
        )

    # uuid4().hex is 32 hex chars — always more than 'remaining' (max needed is 22
    # when SOURCE_ID is a single character), so a plain slice is always safe.
    suffix = uuid.uuid4().hex.upper()[:remaining]
    rrn = f"{prefix}{suffix}"

    assert len(rrn) == RRN_TOTAL_LENGTH, f"Generated RRN is {len(rrn)} chars, expected {RRN_TOTAL_LENGTH}"
    return rrn


def _field_values(field_spec: Dict[str, Any]) -> List[Any]:
    """A field's 'value' may be a scalar or a list — always return it as a list."""
    v = field_spec.get("value")
    if isinstance(v, list):
        return v if v else [None]
    return [v]


def _baseline_value(field_spec: Dict[str, Any]) -> Any:
    """The first value is always treated as the baseline/default correct value."""
    return _field_values(field_spec)[0]


def apply_mandatory_fields(payload: Dict[str, Dict[str, Any]]) -> tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """
    - Auto-injects REQUEST_AUTH_ID / REQUEST_TELLER_ID / BRANCH_CODE with defaults
      if any of them is missing from the input payload (each checked independently).
    - Raises ValueError if SOURCE_ID is missing — this is a hard requirement.
    - REQUEST_REFERENCE_NUMBER (RRN):
        * If the user already provided it in the input payload -> leave it exactly
          as given, it stays a normal payload field and appears in every Test Data.
        * If the user did NOT provide it -> generate it via make_rrn(SOURCE_ID) for
          reference only. It is NOT added to the payload, so it will NOT appear as
          a key in the generated Test Data for any test case.

    Returns: (payload_with_mandatory_fields, generated_rrn_or_None)
    """
    payload = dict(payload)  # shallow copy — don't mutate the caller's dict

    for field_name, default_spec in MANDATORY_DEFAULT_FIELDS.items():
        if field_name not in payload:
            payload[field_name] = dict(default_spec)

    source_spec = payload.get(SOURCE_ID_FIELD)
    if not source_spec or _baseline_value(source_spec) in (None, ""):
        raise ValueError(
            f"'{SOURCE_ID_FIELD}' is mandatory. Please provide {SOURCE_ID_FIELD} in the input payload."
        )

    rrn_spec = payload.get(RRN_FIELD)
    rrn_already_provided = bool(rrn_spec) and _baseline_value(rrn_spec) not in (None, "")

    generated_rrn: Optional[str] = None
    if not rrn_already_provided:
        source_id_value = str(_baseline_value(source_spec))
        generated_rrn = make_rrn(source_id_value)
        # Intentionally NOT added to `payload` — it must not appear in Test Data
        # unless the user supplied it themselves.

    return payload, generated_rrn




def _strip_json_fences(content: str) -> str:
    content = (content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
    return content.replace("```json", "").replace("```", "").strip()


def _sanitize_positive_testcases(
    testcases: List[Dict[str, Any]],
    payload: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Defensive guard for rule #2: if the LLM ever slips and invents a "valid" value
    for a field in a POSITIVE test case that wasn't in the field's given value list,
    force it back to that field's baseline value.
    """
    allowed_values = {
        field: set(str(v) for v in _field_values(spec))
        for field, spec in payload.items()
    }
    baseline = {field: _baseline_value(spec) for field, spec in payload.items()}

    for tc in testcases:
        if tc.get("Test Case Type") != "Positive":
            continue
        td = tc.get("Test Data") or {}
        for field, val in list(td.items()):
            if field in allowed_values and str(val) not in allowed_values[field]:
                td[field] = baseline.get(field, val)
        tc["Test Data"] = td
    return testcases


