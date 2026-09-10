# Code review

Status: **demo** · Provider: `demo` · Model: `deterministic-rules`

DEMO ONLY: deterministic eval/interpolated-SQL pattern checks; no LLM was called.

Snapshot: `a6743399f4c5f45a085d1d66fb259e8d81d10846afb84906f357021446ead276`

Reviewed 2 of 2 changed files.

## HIGH: Review interpolated SQL execution

`customers.py:2` (new side) · Confidence: 0.8

This added line interpolates values into SQL. If a value is caller-controlled, it can alter the SQL statement. Confirm its source.

Suggested fix: Use bound query parameters supported by the database driver.

Evidence: 'return db.execute(f"SELECT id FROM customers WHERE email = \'{email}\'")'

## HIGH: Review dynamic expression evaluation

`pricing.py:5` (new side) · Confidence: 0.8

This added line evaluates a string as code. If the expression is caller-controlled, a caller can execute code in this process. Confirm its source.

Suggested fix: Replace eval with a constrained parser or an explicit operation allowlist.

Evidence: 'return eval(expression)'


## Usage and cost

Conversation: `WEB-123`
Input tokens: 0 · Output tokens: 0
Estimated USD for this call: 0
No remote model charge for this event; local compute/subscription costs excluded.

## Requirements sources

- jira WEB-123: DEMO: Safe pricing and customer lookup (version synthetic-1)
- confluence 42: DEMO: Input validation requirements (version synthetic-1)
- slack CDEMO:1: DEMO: Team decision (version synthetic-1)

Location and evidence validation does not prove a finding is correct.
No code or project tests were executed. A clean report is not approval to merge.
