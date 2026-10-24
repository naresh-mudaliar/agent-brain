# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

---

## 🔒 Security Architecture Highlights

`agent-brain` is designed with local-first security and cryptographic guarantees:

1. **Local-Only Graph Processing**: Knowledge graphs are stored locally on-disk (`./.agent-memory/brain.db`) with embedded Kùzu. No code or AST tokens leave your local environment unless explicitly exported.
2. **Fernet AES Encryption**: Memory export archives (`memory-seed.enc`) are encrypted using Fernet (AES-128-CBC with PKCS7 padding and HMAC-SHA256 authenticated encryption).
3. **Zip Slip Traversal Prevention**: Archive decompression validates absolute canonical extraction paths against target roots, preventing path traversal attacks.
4. **Parameterized openCypher**: All Cypher queries pass arguments through parameterized dictionaries to prevent Cypher injection vulnerabilities.

---

## 🛡️ Reporting a Vulnerability

If you discover a security vulnerability in `agent-brain`, please **do not open a public issue**.

Instead, please report it via private email or GitHub Security Advisory:
- Open a draft security advisory on GitHub under the **Security** tab.
- We will acknowledge receipt within 48 hours and work with you on a coordinated disclosure.
