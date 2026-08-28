import fitz  
import base64
import json
import uuid
import os
from dotenv import load_dotenv
import httpx
import re
from openai import AzureOpenAI
from typing import List, Dict, Any
from dotenv import load_dotenv
load_dotenv()

API_ENDPOINT = os.getenv("AZURE_API_ENDPOINT")
API_VERSION= os.getenv("AZURE_API_VERSION")
API_KEY= os.getenv("AZURE_API_KEY")
MODEL= os.getenv("AZURE_MODEL_NAME")

# --- Optional: Disable SSL verification (not for production!) ---
http_client = httpx.Client(verify=False)

# --- Initialize Azure OpenAI client ---
client = AzureOpenAI(
    azure_endpoint=API_ENDPOINT,
    api_version=API_VERSION,
    api_key=API_KEY,
    http_client=http_client
)



def azure_remove_duplicate_testcases(original_testcases: List[Dict[str, Any]], max_retries=3, retry_delay=2) -> List[int]:
    """
    Remove semantically duplicate test cases from the provided list using Azure's model.
    Includes retry mechanism for failed Azure API calls.
   
    Args:
        original_testcases: List of dictionaries containing test cases with keys:
                          Test Case ID, Test Case Name, Scenario Name, Type,
                          Description, Steps, Test Data, Expected Result, Page No
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Delay in seconds between retries (default: 2)
   
    Returns:
        List[int]: List of indexes to remove (duplicates), empty list if all retries fail
    """
    # Add index key to each test case for LLM clarity
    indexed_testcases = []
    for i, tc in enumerate(original_testcases):
        tc_with_index = {"index": i, **tc}
        indexed_testcases.append(tc_with_index)
   
    # Convert list to JSON string for LLM processing
    testcases_json = json.dumps(indexed_testcases, indent=2, ensure_ascii=False)
   
    system_prompt = """You are an expert test case analyst specializing in identifying and removing duplicate test cases.

## CRITICAL RULE: VERY STRICT DUPLICATE DETECTION

**IMPORTANT**: Mark test cases as duplicates ONLY if they are EXACTLY THE SAME test with just minor wording differences. Be VERY CONSERVATIVE in marking duplicates.

## What Makes Test Cases TRUE Duplicates?

Test cases are TRUE duplicates ONLY if ALL of these are identical:
1. **Same exact field/transaction/module being tested**
2. **Same exact validation or operation being performed**
3. **Same exact test scenario (positive/negative/exceptional)**
4. **Same exact conditions (branch, status, mode, channel, etc.)**
5. **Only difference is rewording - the actual test is 100% identical**

## When to Mark as Duplicate

**Mark as duplicate ONLY when:**
- Both test cases test the EXACT SAME thing
- The ONLY difference is how it's written (synonyms, sentence structure)
- No meaningful difference in what is being verified
- Steps are essentially identical, just reworded

**Example of TRUE Duplicate:**
```
Test A: "Verify field X accepts 17 numeric characters"
Test B: "Verify that field X is 17 numeric digits"
```
Both test EXACTLY the same thing - just worded differently.

## When NOT to Mark as Duplicate (MOST CASES)

**DO NOT mark as duplicate if ANY of these differ:**

1. **Different Conditions:**
   - Home branch ≠ Non-home branch
   - With donor details ≠ Without donor details
   - Status 'C' ≠ Status 'D'
   - Valid data ≠ Invalid data

2. **Different Test Types:**
   - Positive test (success case) ≠ Negative test (error case)
   - Test for acceptance ≠ Test for rejection

3. **Different Validations:**
   - Length validation ≠ Format validation
   - Data type check ≠ Mandatory check
   - Min length ≠ Max length

4. **Different Operations:**
   - Create ≠ Update ≠ Delete
   - Mark hold ≠ Unmark hold
   - Allow ≠ Block/Restrict

5. **Different Transaction Codes:**
   - Transaction 21051 ≠ Transaction 1045
   - Transaction 51073 ≠ Transaction 1010

6. **Different Modes/Channels:**
   - CASH mode ≠ CLRG mode
   - Branch channel ≠ API channel

7. **Different Scenarios/Context:**
   - When X exists ≠ When X is missing
   - From source A ≠ From source B

## Examples - NOT Duplicates

**Example 1 - Different Conditions (NOT DUPLICATE):**
```
Test A: "Verify 21051 transaction allowed from home branch with donor details"
Test B: "Verify 21051 transaction rejected from home branch without donor details"
```
**Different**: One tests WITH donor (positive), other WITHOUT donor (negative) → KEEP BOTH

**Example 2 - Different Branches (NOT DUPLICATE):**
```
Test A: "Verify 21051 transaction from home branch"
Test B: "Verify 21051 transaction from non-home branch"  
```
**Different**: Different branch conditions → KEEP BOTH

**Example 3 - Different Transaction Codes (NOT DUPLICATE):**
```
Test A: "Verify 21051 transaction with donor details"
Test B: "Verify 1045 transaction with donor details"
```
**Different**: Different transaction codes → KEEP BOTH

**Example 4 - Different Status Values (NOT DUPLICATE):**
```
Test A: "Verify amendment blocked when status is 'C'"
Test B: "Verify amendment blocked when status is 'D'"
```
**Different**: Different status values → KEEP BOTH

**Example 5 - Success vs Rejection (NOT DUPLICATE):**
```
Test A: "Verify transaction succeeds with valid account"
Test B: "Verify transaction rejected with invalid account"
```
**Different**: Positive vs negative scenario → KEEP BOTH

**Example 6 - Different Validations (NOT DUPLICATE):**
```
Test A: "Verify field accepts 17 characters (length check)"
Test B: "Verify field accepts only numeric (type check)"
```
**Different**: Length validation vs data type validation → KEEP BOTH

## Simple Rule of Thumb

**Ask yourself: Are these testing two different scenarios or conditions?**
- If YES → NOT duplicates, keep both
- If NO (exact same scenario, just reworded) → Duplicates

**If you have ANY doubt whether they're duplicates → They are NOT duplicates, keep both**

## Analysis Process

For each pair of test cases:

1. **Check Transaction/Field**: Same exact transaction code or field name?
   - If different → NOT DUPLICATE

2. **Check Conditions**: Same branch, status, mode, channel, etc.?
   - If ANY condition differs → NOT DUPLICATE

3. **Check Test Type**: Both positive OR both negative OR both exceptional?
   - If different types → NOT DUPLICATE

4. **Check Operation**: Same operation (allow/block, create/update, etc.)?
   - If different operations → NOT DUPLICATE

5. **Check Scenario Context**: Same scenario (with/without, valid/invalid, exists/missing)?
   - If different contexts → NOT DUPLICATE

6. **Final Check**: If everything above is IDENTICAL, only then mark as duplicate

## Keep One, Remove Others

When you find TRUE duplicates (rare):
- ALWAYS keep the test case with the LOWER index number (earlier in the list)
- Remove the higher-index duplicate(s)
- This preserves the document flow order
- Do NOT keep a later test case over an earlier one, even if it seems more detailed

## Output Format

Return ONLY a JSON array of index numbers to REMOVE.
- If no duplicates found (most common case) → return empty array []
- If duplicates found → return indexes of less detailed ones
- Do NOT include markdown or explanations

Examples:
- No duplicates: []
- Found duplicates at indexes 5 and 12, keep 5: [12]
- Found duplicate group 3,8,15, keep 3: [8, 15]
"""

    user_prompt = f"""Analyze these test cases for duplicates. Be VERY STRICT - mark as duplicates ONLY if they test the EXACT SAME thing with just minor wording differences.

## Test Cases:
{testcases_json}

## Critical Rules:
1. Different conditions (home/non-home branch, with/without details, etc.) = NOT duplicates
2. Different test types (positive/negative) = NOT duplicates
3. Different transaction codes = NOT duplicates
4. Different status/mode values = NOT duplicates
5. Different validations (length/format/type) = NOT duplicates
6. Success vs rejection scenarios = NOT duplicates
7. Different operations (allow/block, create/update) = NOT duplicates

## When in doubt, keep both tests

Return ONLY a JSON array of indexes to remove. If no true duplicates, return [].
No explanations, no markdown."""
   
    last_error = None
   
    for attempt in range(1, max_retries + 1):
        try:
            print(f"    Checking for duplicates (Attempt {attempt}/{max_retries})...")
           
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": user_prompt
                    }
                ],
                temperature=0.1,  # Low temperature for consistency
                max_tokens=15000,  # Adjust based on your test case volume
            )

            content = response.choices[0].message.content.strip()
           
            # Remove markdown code fences if present
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            content = content.replace("```json", "").replace("```", "").strip()

            try:
                duplicate_indexes = json.loads(content)
               
                if not isinstance(duplicate_indexes, list):
                    raise ValueError(f"Response is not a list: {content}")
               
                # Validate all items are integers
                duplicate_indexes = [int(idx) for idx in duplicate_indexes]
               
                # Validate indexes are within range
                valid_indexes = [idx for idx in duplicate_indexes if 0 <= idx < len(original_testcases)]
               
                if len(valid_indexes) != len(duplicate_indexes):
                    print(f"    ⚠ Some indexes were out of range and filtered out")
                    print(f"       Invalid indexes: {set(duplicate_indexes) - set(valid_indexes)}")
               
                # Success - return the valid indexes
                print(f"    ✓ Duplicate check completed successfully")
                return sorted(set(valid_indexes))
               
            except json.JSONDecodeError as je:
                raise Exception(f"Failed to parse JSON response: {je}. Raw content: {content[:200]}")
            except (ValueError, TypeError) as ve:
                raise Exception(f"Invalid index format: {ve}. Raw content: {content[:200]}")
       
        except Exception as e:
            last_error = e
            print(f"    ✗ Duplicate detection error on attempt {attempt}/{max_retries}: {str(e)}")
           
            if attempt < max_retries:
                print(f"    Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print(f"    ✗ All {max_retries} attempts failed. Last error: {last_error}")
                print(f"    ⚠ Returning empty list - no duplicates will be removed")
                return []
   
    # This should never be reached, but just in case
    return []


def process_and_clean_testcases(result):
    """
    Process test cases and remove duplicates using LLM-based semantic analysis.
    Then renumber all Test Case IDs in sequence.
   
    Args:
        result: Dictionary containing 'combined_testcases' key with list of test cases
       
    Returns:
        Updated result dictionary with duplicates removed and Test Case IDs renumbered
    """
    if "combined_testcases" not in result or not result["combined_testcases"]:
        print(f"⚠ No test cases found to clean duplicates from")
        return result
   
    original_count = len(result["combined_testcases"])
    print(f"\n🔄 Starting duplicate removal process...")
    print(f"   Original test case count: {original_count}")
   
    # Get duplicate indexes from LLM
    duplicate_indexes = azure_remove_duplicate_testcases(result["combined_testcases"])
   
    if duplicate_indexes:
        print(f"   Found {len(duplicate_indexes)} duplicates to remove")
        print(f"   Indexes to remove: {duplicate_indexes}")
       
        # Create cleaned list by excluding duplicate indexes
        cleaned_testcases = [
            tc for i, tc in enumerate(result["combined_testcases"])
            if i not in duplicate_indexes
        ]
       
        # Update result
        result["combined_testcases"] = cleaned_testcases
       
        # Update summary statistics
        if "summary" not in result:
            result["summary"] = {}
           
        result["summary"]["original_testcase_count"] = original_count
        result["summary"]["cleaned_testcase_count"] = len(cleaned_testcases)
        result["summary"]["duplicates_removed"] = len(duplicate_indexes)
       
        print(f"✓ Duplicate removal completed")
        print(f"   Removed: {len(duplicate_indexes)} duplicates")
        print(f"   Remaining: {len(cleaned_testcases)} test cases")
    else:
        print(f"✓ No duplicates found - all {original_count} test cases are unique")
       
        # Still update summary for consistency
        if "summary" not in result:
            result["summary"] = {}
        result["summary"]["original_testcase_count"] = original_count
        result["summary"]["cleaned_testcase_count"] = original_count
        result["summary"]["duplicates_removed"] = 0
   

    # ── Sort by page order + functional flow before renumbering ──────────────
    print(f"\n🔄 Sorting test cases by document flow order…")

    def _flow_sort_key(tc: dict) -> tuple:
        """
        Sort key that preserves document flow:
        Primary   — page number (extracted from TC ID like TC_P5_003 → 5)
        Secondary — original index within page (the _003 part → 3)
        Tertiary  — test type order (Positive=0, Negative=1, Exceptional=2)
        """
        tc_id = str(tc.get("Test Case ID", "") or tc.get("Sr.No", ""))

        # Extract page number from ID like TC_P5_003 or TC_P12_007
        page_match = re.search(r"TC_P(\d+)_(\d+)", tc_id)
        if page_match:
            page_num  = int(page_match.group(1))
            seq_num   = int(page_match.group(2))
        else:
            # Already renumbered or different format — preserve current order
            seq_match = re.search(r"(\d+)", tc_id)
            page_num  = 999
            seq_num   = int(seq_match.group(1)) if seq_match else 999

        # Test type ordering: Positive before Negative before Exceptional
        tc_type = str(
            tc.get("Type") or
            tc.get("Positive / Negative") or
            ""
        ).lower()
        type_order = 0 if "positive" in tc_type else (1 if "negative" in tc_type else 2)

        return (page_num, seq_num, type_order)

    import re as _re_sort
    try:
        result["combined_testcases"].sort(key=_flow_sort_key)
        print(f"✓ Test cases sorted by document flow order")
    except Exception as e:
        print(f"⚠ Sort failed ({e}) — preserving existing order")

    # ── Renumber after sorting ────────────────────────────────────────────────
    print(f"🔄 Renumbering Test Case IDs…")
    for i, tc in enumerate(result["combined_testcases"], start=1):
        tc["Test Case ID"] = f"TC_{i:03d}"

    print(f"✓ Test Case IDs renumbered: TC_001 to TC_{len(result['combined_testcases']):03d}")
   
   
    return result
