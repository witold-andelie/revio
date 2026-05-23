# Security Checklist for Code Review

## Input Validation

- [ ] All user input is validated before processing
- [ ] Input length limits are enforced
- [ ] Input format is validated (email, URL, etc.)
- [ ] File uploads are validated (type, size, content)
- [ ] Path traversal is prevented (no `../` in file paths)

## SQL Injection Prevention

- [ ] Parameterized queries are used for all database operations
- [ ] No string formatting or concatenation in SQL queries
- [ ] ORM is used correctly (avoid raw SQL when possible)
- [ ] Database user has minimal required permissions

## XSS Prevention

- [ ] User input is escaped before rendering in HTML
- [ ] Content Security Policy headers are set
- [ ] HTTP-only and Secure flags on cookies
- [ ] Output encoding matches the context (HTML, JS, URL)

## Authentication & Authorization

- [ ] Passwords are hashed with bcrypt, argon2, or scrypt
- [ ] No plaintext password storage
- [ ] Session tokens are cryptographically random
- [ ] Sessions expire after reasonable timeout
- [ ] Rate limiting on login attempts
- [ ] Authorization checks on all protected endpoints

## Secrets Management

- [ ] No hardcoded API keys, passwords, or tokens
- [ ] Secrets loaded from environment variables or secrets manager
- [ ] Secrets are not logged or included in error messages
- [ ] `.env` files are in `.gitignore`

## Cryptography

- [ ] No MD5 or SHA1 for security purposes
- [ ] Use AES-256 for symmetric encryption
- [ ] Use RSA-2048+ or Ed25519 for asymmetric encryption
- [ ] Use cryptographically secure random number generator (`secrets` module)
- [ ] TLS 1.2+ for all network communications

## Error Handling

- [ ] Error messages don't leak internal details
- [ ] Stack traces are not exposed to users
- [ ] Sensitive data is not included in logs
- [ ] Fail securely (deny by default)

## Dependency Security

- [ ] Dependencies are pinned to specific versions
- [ ] Known vulnerable dependencies are updated
- [ ] Only use trusted package sources
- [ ] Regular dependency audits

## API Security

- [ ] Rate limiting is implemented
- [ ] Request size limits are enforced
- [ ] CORS is configured correctly
- [ ] API keys are rotated regularly
- [ ] Input validation on all API parameters
