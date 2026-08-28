"""
api/utils/feature_file_utils.py
Generates Gherkin .feature files from generated test cases using Azure OpenAI.
"""
import os
import json
import re
import time
from typing import List, Dict, Optional
from openai import AzureOpenAI
import httpx
from dotenv import load_dotenv

load_dotenv()

_az = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_API_ENDPOINT", ""),
    api_version=os.getenv("AZURE_API_VERSION", "2024-12-01-preview"),
    api_key=os.getenv("AZURE_API_KEY", ""),
    http_client=httpx.Client(verify=False),
)
_CHAT_MODEL = os.getenv("AZURE_CHAT_MODEL", os.getenv("AZURE_MODEL_NAME", "gpt-4.1-mini"))

FEATURE_MAX_TOKENS = 16000
TRADE_FINANCE_DEPT_ID = "171"

STANDARD_COLUMNS = [
    "Test Case ID",
    "Test Case Name",
    "Scenario Name",
    "Type",
    "Description",
    "Steps",
    "Test Data",
    "Expected Result",
]

TRADE_FINANCE_COLUMNS = [
    "Test Case ID",
    "Function Description",
    "Sub Function Description",
    "Pre-Condition",
    "Test Case Description",
    "Expected Result",
    "Priority",
    "Positive / Negative",
]


def _select_columns(testcases: List[Dict], department_id: Optional[str]) -> List[Dict]:
    is_tf = str(department_id or "").strip() == TRADE_FINANCE_DEPT_ID
    cols  = TRADE_FINANCE_COLUMNS if is_tf else STANDARD_COLUMNS
    result = []
    for tc in testcases:
        filtered = {k: tc[k] for k in cols if k in tc}
        result.append(filtered)
    return result


def _build_feature_prompt(
    testcases_json: str,
    document_name:  str,
    department_id:  Optional[str],
    testcase_client: str,
) -> str:
    is_tf     = str(department_id or "").strip() == TRADE_FINANCE_DEPT_ID
    dept_hint = "Trade Finance banking system" if is_tf else "Indian banking system"

    return f"""You are a senior QA automation engineer specializing in {dept_hint} applications.
Your task is to convert the provided test cases into a valid Gherkin .feature file.

## Document: {document_name}
## Test Environment: {testcase_client}

## Input Test Cases (JSON):
{testcases_json}

## Rules for generating the Gherkin feature file:

1. **Feature Block**: Create one Feature block at the top. Derive a meaningful feature name
   from the document name and test case scenarios.

2. **Background**: Add a Background block only for preconditions shared across most scenarios
   (e.g. login, screen navigation). Keep it minimal.

3. **Scenario vs Scenario Outline**:
   - Use `Scenario:` for individual test cases.
   - Use `Scenario Outline:` + `Examples:` ONLY when multiple test cases share identical
     steps but differ only in data values (e.g. field length boundary tests).

4. **Step Keywords**:
   - `Given`  — preconditions and system state
   - `When`   — user actions and API calls
   - `Then`   — assertions and expected outcomes
   - `And` / `But` — continuation of the same keyword type

5. **Step Writing Rules**:
   - Every step must be one clear, executable action or assertion.
   - Use exact field names, button labels, and screen numbers from the test cases.
   - Include specific test data values from the Steps / Test Data / Pre-Condition columns.
   - Expected results must be specific: exact error messages, status values, reference numbers.
   - NEVER write vague steps such as "the system behaves correctly" or "the page loads".

6. **Scenario Naming**:
   - Standard departments : use Test Case Name as the scenario title.
   - Trade Finance        : use Sub Function Description as the scenario title.

7. **Tags** (one line, space-separated, before each Scenario):
   - @{testcase_client}
   - @Positive | @Negative | @Exceptional  (from Type or Positive/Negative field)
   - @TC_XXX  (matching Test Case ID)
   - @High    (Trade Finance only, for Priority = High)

8. **Data Tables**: Use Gherkin pipe-delimited data tables for structured test data where
   multiple rows share the same structure.

9. **Output Format**:
   - Return ONLY raw Gherkin content — no markdown fences, no JSON, no explanations.
   - Start the response directly with the `Feature:` keyword.
   - Leave one blank line between every Scenario / Scenario Outline block.
   - Valid Gherkin syntax only.

## Example structure:

Feature: Fund Transfer Validation
  As a bank customer
  I want to transfer money between accounts
  So that I can manage my funds conveniently

  Background:
    Given the user is logged into the banking application
    And the user navigates to the Fund Transfer screen

  @{testcase_client} @Positive @TC_001
  Scenario: Successful fund transfer with valid amount
    Given the source account "SB-123456789" has a balance of Rs 50000
    When the user enters beneficiary account "SB-987654321"
    And enters transfer amount "5000"
    And clicks the "Transfer" button
    Then the system displays "Transaction successful"
    And the source account balance is updated to Rs 45000
    And a transaction reference number is generated

  @{testcase_client} @Negative @TC_002
  Scenario: Transfer amount exceeds available balance
    Given the source account "SB-123456789" has a balance of Rs 1000
    When the user enters transfer amount "50000"
    And clicks the "Transfer" button
    Then the system displays error "Insufficient Balance"
    And the transfer is not processed

  @{testcase_client} @Negative @TC_010
  Scenario Outline: Account number field length validation
    Given the user is on the Fund Transfer screen
    When the user enters "<account_no>" in the "Beneficiary Account Number" field
    And clicks "Validate"
    Then the system displays "<expected_message>"

    Examples:
      | account_no           | expected_message                          |
      | 1234567890123456     | Account number must be 17 digits          |
      | 123456789012345678   | Account number must be 17 digits          |

Now generate the complete .feature file for ALL provided test cases.
Return ONLY valid Gherkin — start directly with Feature:"""


def generate_feature_file(
    testcases:       List[Dict],
    document_name:   str,
    department_id:   Optional[str] = None,
    testcase_client: str           = "UAT",
    max_retries:     int           = 3,
    retry_delay:     int           = 2,
) -> str:
    if not testcases:
        raise ValueError("No test cases provided for feature file generation.")

    filtered = _select_columns(testcases, department_id)

    MAX_TC = 200
    if len(filtered) > MAX_TC:
        print(f"  ⚠ Feature file: truncating to {MAX_TC} test cases (received {len(filtered)})")
        filtered = filtered[:MAX_TC]

    testcases_json = json.dumps(filtered, indent=2, ensure_ascii=False)
    prompt         = _build_feature_prompt(testcases_json, document_name, department_id, testcase_client)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"  🥒 Feature file generation attempt {attempt}/{max_retries}…")
            response = _az.chat.completions.create(
                model    = _CHAT_MODEL,
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are a senior QA automation engineer. "
                            "Generate valid Gherkin .feature file content only. "
                            "No markdown, no code fences, no explanations. "
                            "Start directly with the Feature: keyword."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature = 0.3,
                max_tokens  = FEATURE_MAX_TOKENS,
            )

            content       = (response.choices[0].message.content or "").strip()
            finish_reason = response.choices[0].finish_reason

            # Strip accidental markdown fences
            content = re.sub(r"^```(gherkin|feature|plain)?", "", content, flags=re.IGNORECASE).strip()
            content = re.sub(r"```$", "", content).strip()

            if not content.startswith("Feature:"):
                idx = content.find("Feature:")
                if idx >= 0:
                    content = content[idx:]
                else:
                    raise ValueError(
                        f"LLM response does not start with Feature:. "
                        f"finish_reason={finish_reason}. "
                        f"First 200 chars: {content[:200]}"
                    )

            print(f"  ✅ Feature file generated: {len(content)} chars, finish_reason={finish_reason}")
            return content

        except Exception as e:
            last_error = e
            print(f"  ✗ Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)

    raise RuntimeError(
        f"Feature file generation failed after {max_retries} attempts. "
        f"Last error: {last_error}"
    )
