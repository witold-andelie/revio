# Python Best Practices

## PEP 8 Highlights

### Naming Conventions
- **Functions and variables**: `snake_case` — `calculate_total`, `user_count`
- **Classes**: `PascalCase` — `UserService`, `DatabaseConnection`
- **Constants**: `UPPER_SNAKE_CASE` — `MAX_RETRIES`, `DEFAULT_TIMEOUT`
- **Private members**: prefix with underscore — `_internal_method`, `_cache`
- **Dunder methods**: reserved for Python — `__init__`, `__str__`

### Imports
- One import per line (not `from os import path, getcwd`)
- Group imports: stdlib → third-party → local
- Use absolute imports over relative imports
- Avoid wildcard imports (`from module import *`)

### Line Length
- Maximum 79 characters for code
- Maximum 72 characters for docstrings/comments
- Use parentheses for line continuation, not backslashes

## Common Antipatterns

### Mutable Default Arguments
```python
# BAD
def add_item(item, items=[]):
    items.append(item)
    return items

# GOOD
def add_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items
```

### Bare Except Clauses
```python
# BAD
try:
    risky_operation()
except:
    pass

# GOOD
try:
    risky_operation()
except ValueError as e:
    logger.error(f"Value error: {e}")
```

### String Formatting in Queries
```python
# BAD — SQL injection risk
query = f"SELECT * FROM users WHERE id={user_id}"

# GOOD — parameterized query
query = "SELECT * FROM users WHERE id=?"
cursor.execute(query, (user_id,))
```

## Type Hints

### Function Signatures
```python
def process_data(
    items: list[dict[str, Any]],
    threshold: float = 0.5,
    verbose: bool = False,
) -> tuple[list[Result], int]:
    """Process items and return results with count."""
    ...
```

### Optional Values
```python
# Python 3.10+
def find_user(name: str) -> dict | None:
    ...

# Python 3.9 and earlier
from typing import Optional
def find_user(name: str) -> Optional[dict]:
    ...
```

## Context Managers

Always use context managers for resource management:
```python
# GOOD
with open("file.txt") as f:
    content = f.read()

# GOOD — custom context manager
with database_connection() as conn:
    conn.execute(query)
```

## List Comprehensions

Use comprehensions for simple transformations:
```python
# GOOD
squares = [x**2 for x in range(10)]
even_squares = [x**2 for x in range(10) if x % 2 == 0]

# BAD — too complex, use a regular loop
result = [transform(x) for x in data if condition(x) and other_condition(x) and validate(x)]
```
