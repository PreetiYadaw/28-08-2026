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


API_TESTCASE_SYSTEM_PROMPT = """You are an expert API test analyst for a banking application (State Bank of India).
You generate POSITIVE and NEGATIVE test cases for a SINGLE REST API from a payload
specification that gives, for every field: its correct value(s), whether it is
required, and a plain-English validation rule (may be empty).

## VALUE FORMAT
Each field's "value" is EITHER:
  - a single value (e.g. "value": "9876543210"), OR
  - a LIST of multiple values (e.g. "value": ["9876543210", "9123456780"])
When it is a list, EVERY entry in that list is an independently CORRECT / VALID value
for that field — not a range, not an example to interpolate from, just a fixed set of
acceptable values.

## STRICT RULES — FOLLOW EXACTLY

1. BASELINE POSITIVE TEST CASE
   - Always create exactly ONE positive test case using the FIRST value of every
     field's "value" (if it's a list, use value[0]; if scalar, use it as-is).
   - Description example: "To verify <api_name> with correct payload fetches correct
     details from the API."

2. NEVER INVENT A "CORRECT" VALUE — CRITICAL
   - For POSITIVE test cases, you may ONLY use a value for a field IF that exact value
     already appears in that field's given "value" list (or is its scalar value).
   - You must NEVER invent, guess, or construct a new value that you believe would
     also be valid — even if it seems like it satisfies the stated validation rule.
   - If a field's "value" is a list with MORE THAN ONE entry, you MAY create one
     additional POSITIVE test case per EXTRA entry in that list (varying ONLY that
     field, all other fields at their baseline/first value). Description: "To verify
     <api_name> accepts a valid <field> (<value>) and processes the request
     successfully."
   - If a field's "value" is a single scalar (or a list with only ONE entry), do NOT
     create any additional positive variant for that field — only the baseline covers
     it.
   - This restriction applies ONLY to POSITIVE test cases. Negative test cases are
     expected to use invalid/broken values by design (see rule 4).

3. VARY ONLY ONE FIELD AT A TIME — NEVER COMBINE
   - Every additional test case (positive variant or negative) changes ONLY ONE field
     from its baseline value. ALL other fields MUST keep their baseline (first) value.
   - Do NOT generate permutations or combinations across multiple fields.

4. NEGATIVE TEST CASES (only for fields with a non-empty "validation")
   - If required=true: exactly ONE negative test case with that field's KEY REMOVED
     from the payload entirely (this tests "mandatory field missing" — do not send an
     empty string for this case, actually omit the key).
   - 1 to 3 further NEGATIVE test cases, each breaking exactly ONE distinct aspect of
     the stated validation rule (e.g. for "10 digit numeric": too short, too long,
     contains alphabets). Pick the most realistic and relevant ones — do not invent
     rules that were not stated.
   - Fields with an empty/missing "validation" and required=false are skipped
     entirely — do not guess rules for them.

5. FUNCTION DESCRIPTION
   - "Function Description" is the API's name/purpose and must be IDENTICAL across
     every test case in this batch, e.g. "Mobile Number Enquiry API".

6. TEST CASE DESCRIPTION — must state field + rule being verified
   - Positive baseline: "To verify <api_name> with correct payload fetches correct
     details from the API."
   - Positive field variant (only when field has multiple valid values): "To verify
     <api_name> accepts a valid <field> and processes the request successfully."
   - Negative: "To verify <api_name> with <field> <plain description of the
     violation> displays an error."
     e.g. "To verify Mobile Enquiry API with RRN greater than 25 characters displays
     an error."
     e.g. "To verify Mobile Enquiry API with mandatory field mobile_number missing
     displays an error."

7. TEST DATA
   - "Test Data" is the COMPLETE JSON payload object (all fields, using their
     baseline values unless that field is the one under test) for that specific test
     case, with the single deliberate change applied (or no change, for the
     baseline). It must be valid JSON.

8. OUTPUT FORMAT — JSON ONLY, NOTHING ELSE
   Return ONLY a JSON array. Each element must have EXACTLY these keys:
     "Function Description": string
     "Test Case Description": string
     "Test Case Type": "Positive" or "Negative"
     "Test Data": object (the full payload for this test case)
   Do NOT include "Test Case ID", "API_URL", or "API_METHOD" — those are added by the
   caller. Do not wrap the array in another object. Do not add markdown fences or
   commentary."""

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


def generate_api_testcases_via_llm(
    api_name: str,
    api_url: str,
    method: str,
    payload: Dict[str, Any],
    user_prompt: Optional[str] = None,
    testcase_type: str = "UAT",
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)

    user_message = (
        f"Generate positive and negative test cases for the following API.\n\n"
        f"API Name: {api_name}\n"
        f"API URL: {api_url}\n"
        f"HTTP Method: {method.upper()}\n"
        f"Test Environment: {testcase_type}\n\n"
        f"Payload specification (field -> value(s) / required / validation):\n"
        f"{payload_json}\n"
    )
    if user_prompt:
        user_message += f"\nAdditional instructions from the user:\n{user_prompt}\n"
    user_message += (
        "\nFollow all rules from the system prompt strictly. Never invent a 'valid' "
        "value that isn't already listed for a field. Vary ONLY one field per test "
        "case. Return the JSON array only."
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"    Generating API test cases (attempt {attempt}/{max_retries})…")
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": API_TESTCASE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.2,
                max_tokens=32000,
            )
            content = _strip_json_fences(response.choices[0].message.content)
            parsed = json.loads(content)
            if not isinstance(parsed, list):
                raise ValueError("LLM response is not a JSON array")

            cleaned: List[Dict[str, Any]] = []
            for idx, tc in enumerate(parsed, start=1):
                if not isinstance(tc, dict):
                    continue
                td = tc.get("Test Data")
                if isinstance(td, str):
                    try:
                        td = json.loads(td)
                    except Exception:
                        pass
                tc_type = str(tc.get("Test Case Type", "")).strip().capitalize()
                if tc_type not in ("Positive", "Negative"):
                    tc_type = "Negative" if "error" in str(tc.get("Test Case Description", "")).lower() else "Positive"
                cleaned.append({
                    "Function Description": tc.get("Function Description", api_name),
                    "Test Case ID": f"TC_API_{idx:03d}",
                    "Test Case Description": str(tc.get("Test Case Description", "")).strip(),
                    "Test Case Type": tc_type,
                    "API_URL": api_url,
                    "API_METHOD": method.upper(),
                    "Test Data": td if isinstance(td, dict) else {},
                })

            if not cleaned:
                raise ValueError("No valid test cases parsed from LLM response")

            cleaned = _sanitize_positive_testcases(cleaned, payload)

            # Re-number IDs sequentially after sanitization (order unchanged, just clarity)
            for i, tc in enumerate(cleaned, start=1):
                tc["Test Case ID"] = f"TC_API_{i:03d}"

            print(f"    ✓ Generated {len(cleaned)} API test cases")
            return cleaned

        except Exception as e:
            last_error = e
            print(f"    ✗ Attempt {attempt} failed: {e}")

    raise RuntimeError(f"API test case generation failed after {max_retries} attempts: {last_error}")



# ============================================================================
# Pass/Fail evaluation (post Run-Automation)
# ============================================================================
EVALUATE_TESTCASE_SYSTEM_PROMPT = """You are an expert API test analyst for a banking application (State Bank of India).
You are given a batch of already-executed API test cases. For each one you receive:
  - "Test Case Description": what the test case is supposed to verify
  - "Test Case Type": "Positive" or "Negative"
  - "Actual_Response": the real response text captured after actually calling the API

Decide whether each test case PASSED or FAILED, based ONLY on whether the actual
response matches what the description says should happen for that test type.

## RULES

1. POSITIVE test cases PASS when:
   - The actual response indicates SUCCESS — e.g. RESPONSE_STATUS "0", correct
     customer/business data returned, no ERROR_CODE / ERROR_DESCRIPTION present.
   They FAIL when the actual response shows an error/rejection instead of success.

2. NEGATIVE test cases PASS when:
   - The actual response indicates the API correctly REJECTED the invalid input —
     e.g. RESPONSE_STATUS "1" or "2", a non-empty ERROR_CODE / ERROR_DESCRIPTION,
     or any "GATEWAY ERROR [...]" message describing a validation rejection.
   They FAIL when the actual response indicates the API incorrectly ACCEPTED the
   bad input and returned success/business data anyway (this is a real bug — the
   validation that should have blocked it did not).

3. If the Actual_Response is empty, or begins with "ERROR:" describing a network,
   timeout, decryption, or key-loading failure (NOT a business/gateway rejection),
   mark it FAILED with a Remarks note that execution itself did not complete
   (this is inconclusive, not a validation success or failure — but for pass/fail
   purposes it must be treated as FAILED so it gets flagged for manual review).

4. Judge each test case independently. Do not let one test case's error code
   influence your judgment of another.

5. Be objective — do not assume test intent beyond what "Test Case Description"
   and "Test Case Type" literally state. If the actual response is ambiguous
   relative to the description, prefer FAILED with a Remarks note explaining the
   ambiguity, rather than guessing PASS.

## OUTPUT FORMAT — JSON ONLY, NOTHING ELSE
Return ONLY a JSON array, one element per input test case, in the SAME ORDER as
given, with EXACTLY these keys:
  "Test Case ID": string (copy from input, unchanged)
  "Pass_Fail": "Pass" or "Fail"
  "Remarks": string (ONE short sentence explaining the verdict, referencing what
             the actual response actually showed)
Do not add markdown fences or commentary. Do not omit any test case."""


def evaluate_api_testcases_via_llm(
    testcases: List[Dict[str, Any]],
    max_retries: int = 3,
) -> Dict[str, Dict[str, str]]:
    """
    Sends (Test Case ID, Test Case Description, Test Case Type, Actual_Response) for
    every test case in ONE batched LLM call and gets back a Pass/Fail verdict + short
    remark for each. Returns a dict keyed by Test Case ID for easy lookup by the
    caller: { "TC_API_001": {"Pass_Fail": "Pass", "Remarks": "..."} , ... }
    """
    slim_input = [
        {
            "Test Case ID": tc.get("Test Case ID", ""),
            "Test Case Description": tc.get("Test Case Description", ""),
            "Test Case Type": tc.get("Test Case Type", ""),
            "Actual_Response": str(tc.get("Actual_Response", "")),
        }
        for tc in testcases
    ]

    user_message = (
        "Evaluate the following executed API test cases and return a Pass/Fail "
        "verdict for each, following the system prompt rules strictly.\n\n"
        f"{json.dumps(slim_input, indent=2, ensure_ascii=False)}\n\n"
        "Return the JSON array only, same order, one entry per test case."
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"    Evaluating pass/fail (attempt {attempt}/{max_retries})…")
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": EVALUATE_TESTCASE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=16000,
            )
            content = _strip_json_fences(response.choices[0].message.content)
            parsed = json.loads(content)
            if not isinstance(parsed, list):
                raise ValueError("LLM response is not a JSON array")

            result: Dict[str, Dict[str, str]] = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                tc_id = str(item.get("Test Case ID", "")).strip()
                if not tc_id:
                    continue
                verdict = str(item.get("Pass_Fail", "")).strip().capitalize()
                if verdict not in ("Pass", "Fail"):
                    verdict = "Fail"
                result[tc_id] = {
                    "Pass_Fail": verdict,
                    "Remarks": str(item.get("Remarks", "")).strip(),
                }

            if not result:
                raise ValueError("No valid verdicts parsed from LLM response")

            print(f"    ✓ Evaluated {len(result)} test cases")
            return result

        except Exception as e:
            last_error = e
            print(f"    ✗ Attempt {attempt} failed: {e}")

    raise RuntimeError(f"Pass/Fail evaluation failed after {max_retries} attempts: {last_error}")
