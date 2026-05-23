# Clean Code Principles

## SOLID Principles

### Single Responsibility Principle (SRP)
Each class/module should have one reason to change.

```python
# BAD — handles both data and formatting
class UserReport:
    def get_data(self): ...
    def format_html(self): ...
    def send_email(self): ...

# GOOD — separate concerns
class UserData:
    def get_data(self): ...

class ReportFormatter:
    def format_html(self, data): ...

class EmailSender:
    def send(self, content): ...
```

### Open/Closed Principle (OCP)
Open for extension, closed for modification.

```python
# BAD — must modify to add new shapes
def area(shape):
    if shape.type == "circle":
        return math.pi * shape.radius ** 2
    elif shape.type == "square":
        return shape.side ** 2

# GOOD — extend by adding new classes
class Shape(ABC):
    @abstractmethod
    def area(self) -> float: ...

class Circle(Shape):
    def area(self) -> float:
        return math.pi * self.radius ** 2
```

### Liskov Substitution Principle (LSP)
Subtypes must be substitutable for their base types.

### Interface Segregation Principle (ISP)
Don't force clients to depend on methods they don't use.

### Dependency Inversion Principle (DIP)
Depend on abstractions, not concretions.

## DRY (Don't Repeat Yourself)

- Extract common logic into shared functions/modules
- Use configuration over duplication
- If you copy-paste code more than twice, refactor it

## KISS (Keep It Simple, Stupid)

- Prefer simple, readable solutions
- Avoid premature optimization
- Don't add complexity for hypothetical future needs

## YAGNI (You Aren't Gonna Need It)

- Don't build features until they're actually needed
- Remove dead code instead of commenting it out
- Keep the codebase lean

## Function Design

### Small Functions
- Functions should do one thing
- Functions should be short (ideally < 20 lines)
- If a function needs a comment to explain what it does, it's too complex

### Function Arguments
- Fewer arguments are better (0–2 ideal, 3 max)
- Use keyword arguments for clarity
- Group related arguments into objects

```python
# BAD
def create_user(name, email, age, address, phone, role): ...

# GOOD
def create_user(profile: UserProfile, role: UserRole): ...
```

## Comments

### Good Comments
- Explain WHY, not WHAT
- Document non-obvious constraints
- Warn about consequences

### Bad Comments
- Commented-out code (delete it — it's in git)
- Obvious comments (`i += 1  # increment i`)
- Misleading comments that don't match the code

## Error Handling

- Use exceptions for exceptional conditions, not control flow
- Create custom exception hierarchies for your domain
- Always clean up resources (context managers)
- Log errors with enough context to debug

## Testing

- Write tests before fixing bugs (reproduce the bug)
- Test edge cases and boundary conditions
- Use descriptive test names that explain the scenario
- Each test should test one thing
