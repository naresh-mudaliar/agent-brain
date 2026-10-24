# Contributing to agent-brain 🧠

Thank you for your interest in contributing to `agent-brain`! We welcome contributions, bug reports, feature suggestions, and documentation improvements.

---

## 🛠️ Development Setup

### 1. Prerequisites
- Python 3.10, 3.11, or 3.12
- Git

### 2. Fork and Clone
```bash
git clone https://github.com/agent-brain/agent-brain.git
cd agent-brain
```

### 3. Virtual Environment & Dependencies
```bash
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate

# Install package in editable mode with development dependencies:
pip install -e .[dev]
```

---

## 🧪 Testing

We require all pull requests to pass the full automated test suite without regressions.

```bash
# Run unit tests via standard unittest
python -m unittest discover -s tests

# Or run specific test modules
python -m unittest tests/test_database.py
python -m unittest tests/test_ast_parser.py
python -m unittest tests/test_mcp_server.py
```

---

## 📐 Code Style & Architecture Guidelines

1. **Native Tree-sitter AST**: We avoid regex heuristics for code parsing. Any new language parser must be implemented using native `tree-sitter` node traversals.
2. **Kùzu Embedded Graph Storage**: Database operations must use parameterized openCypher queries (`$param`) through `Database.execute_query()`.
3. **Model Context Protocol (MCP)**: Any new tools must be registered on the MCP server in `tools/mcp_server.py` with typed schemas and serialized responses.
4. **Security**: Preserve Zip-Slip path traversal guards and AES-Fernet encryption in `sync/s3_sync.py`.

---

## 🚀 Submitting a Pull Request

1. Create a feature branch: `git checkout -b feature/my-new-feature`
2. Commit your changes: `git commit -m "feat: add support for Kotlin"`
3. Push to your fork: `git push origin feature/my-new-feature`
4. Open a Pull Request on GitHub with a clear summary of your changes and test results.
